"""Offline generic PDF publication helpers shared by the e2e, catalog and HTTP tests."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from enterprise_pdf_rag.adapters.document_store import LocalDocumentStore
from enterprise_pdf_rag.adapters.draft_publication import (
    DraftPublication,
    index_draft,
    publish_draft,
    qualify_draft,
)
from enterprise_pdf_rag.adapters.pdf_ingestion import IngestionSummary, ingest_pdf
from enterprise_pdf_rag.adapters.processing_retrieval import resolve_processing_context
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.processing.retrieval import PinnedRetrievalHit, RetrievalContext
from ragspine.extraction.evidence.figures.ports import EmbeddingPort
from ragspine.extraction.evidence.page.models import ObjectKind
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import (
    DEFAULT_TABLE,
    DIAGRAM_NODES,
    DIAGRAM_REGION,
    FORMULA_FRACTION_BBOX,
    FORMULA_POWER_BBOX,
    TABLE_COLUMNS,
    TABLE_ROWS,
    TableSpec,
    authored_pdf,
)

PROVIDER_BASE_URL = "https://provider.invalid"
# Kept short so the authored line fits the 240pt page width (no overflow/truncation).
DOCUMENT_LABEL = "Revenue expense ratio"
# The ruled grid of ``authored_pdf(table_page=True)``.
TABLE_BBOX = (TABLE_COLUMNS[0], TABLE_ROWS[0], TABLE_COLUMNS[-1], TABLE_ROWS[-1])
# The natural-language branch of the Diagram object; its words are never a claim path.
DIAGRAM_DESCRIPTION = "Two boxes joined by an arrow."
# The two authored formula regions of ``authored_pdf(formula_page=True)`` and the
# natural-language branch of a Formula object, which is lineage only: the qualified
# description is the proof's own readable transcription.
FORMULA_REGIONS = {"fraction": FORMULA_FRACTION_BBOX, "power": FORMULA_POWER_BBOX}
FORMULA_DESCRIPTION = "A formula defining ROE as Net profit over Equity."
_INTERPRETATION = {
    "Text": "Body financial narrative",
    "Table": "Ruled metrics table",
    "Diagram": "Two labelled frames joined by one arrow",
    "Formula": "An authored expression with its drawn rule",
}


def table_region(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """The layout region the stub partitioner proposes around a ruled grid (5pt margin)."""
    return (bbox[0] - 5.0, bbox[1] - 5.0, bbox[2] + 5.0, bbox[3] + 5.0)


def _spec(table_page: bool | TableSpec) -> TableSpec | None:
    if isinstance(table_page, TableSpec):
        return table_page
    return DEFAULT_TABLE if table_page else None


def _extent(observations: list[dict[str, Any]]) -> list[float]:
    # Bind the region to the actual span extents so the literal projection stays
    # in bounds regardless of how the embedded font renders the line. A real layout
    # model echoes the prompt's coordinates in their shortest decimal form
    # (``42.400000000000006`` becomes ``42.4``), so the stub does the same.
    boxes = [
        [round(float(value), 6) for value in observation["bbox"]] for observation in observations
    ]
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _region(region_id: str, kind: str, bbox: list[float], span_ids: list[str]) -> dict[str, object]:
    return {
        "region_id": region_id,
        "kind": kind,
        "bbox": bbox,
        "source_span_ids": span_ids,
        "context_span_ids": [],
        "list_items": [],
        "list_ordered": None,
        "parent_id": None,
        "interpretation": _INTERPRETATION[kind],
    }


def _inside(observation: dict[str, Any], box: tuple[float, ...]) -> bool:
    bbox = [float(value) for value in observation["bbox"]]
    return box[0] <= bbox[0] and box[1] <= bbox[1] and bbox[2] <= box[2] and bbox[3] <= box[3]


def _diagram_ir_reply(prompt: str) -> dict[str, Any]:
    """A diagram-observations-v1 answer copying the two authored labels verbatim."""
    view: dict[str, Any] = json.loads(prompt.rsplit("\n", 1)[-1])
    by_text = {str(item["text"]): str(item["id"]) for item in view["observations"]}
    nodes = [
        {
            "node_id": node_id,
            "label": label,
            "bbox": list(rect),
            "evidence": {"element_ids": [by_text[label]], "confidence": "high"},
        }
        for node_id, (rect, label, _origin) in DIAGRAM_NODES.items()
    ]
    return {
        "schema_version": "diagram-observations-v1",
        "svg_digest": view["svg_digest"],
        "nodes": nodes,
        "edges": [
            {
                "source_node_id": "n1",
                "target_node_id": "n2",
                "label": None,
                "relationship": "leads to",
                "evidence": {"element_ids": [], "confidence": "high"},
            }
        ],
        "confidence": "high",
        "diagnostics": [],
    }


def _formula_ir_reply(prompt: str) -> dict[str, Any]:
    """A formula-observations-v1 answer citing every observation the region owns."""
    view: dict[str, Any] = json.loads(prompt.rsplit("\n", 1)[-1])
    return {
        "schema_version": "formula-observations-v1",
        "svg_digest": view["svg_digest"],
        "source_literal_element_ids": [str(item["id"]) for item in view["observations"]],
        "normalization_state": "inferred",
        "latex": "ROE = \\frac{Net profit}{Equity}",
        "confidence": "medium",
        "diagnostics": [],
    }


def _visual_description_reply(prompt: str, text: str) -> dict[str, Any]:
    view: dict[str, Any] = json.loads(prompt.rsplit("\n", 1)[-1])
    return {
        "schema_version": "visual-description-v1",
        "svg_digest": view["svg_digest"],
        "text": text,
        "evidence": {
            "element_ids": [str(item["id"]) for item in view["observations"]],
            "confidence": "0.5",
        },
        "diagnostics": [],
    }


def text_partition_sender(
    calls: list[bytes],
    *,
    table_caption: bool = False,
    table_bbox: tuple[float, float, float, float] = TABLE_BBOX,
    diagram_page: bool = False,
    diagram_caption: bool = False,
    formula_page: bool = False,
) -> Callable[..., bytes]:
    """Classify page regions offline: occurrences inside the authored ruled grid become one
    Table region, those inside the authored diagram frame one Diagram region, those inside
    each authored formula box one Formula region, everything else one Text region.

    ``table_caption`` also hands the page's caption line to the Table region, which
    leaves the region owning an occurrence outside its native cells; ``diagram_caption``
    does the same for the Diagram region, leaving a source occurrence no node cites.
    ``table_bbox`` is the authored grid to classify against, for fixtures other than the
    default one. A Diagram region does fire the two visual model calls, which this stub
    answers from the observations it was shown; nothing else reaches a provider seam.
    """
    region = table_region(table_bbox)

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url == f"{PROVIDER_BASE_URL}/v1/chat/completions"
        calls.append(payload)
        prompt = json.loads(payload)["messages"][1]["content"][0]["text"]
        if "Return diagram-observations-v1" in prompt:
            return _reply(_diagram_ir_reply(prompt))
        if "Return formula-observations-v1" in prompt:
            return _reply(_formula_ir_reply(prompt))
        if "Return visual-description-v1" in prompt:
            # The two visual branches share one prompt shape; the authored layouts are
            # mutually exclusive, so the page under test names the region.
            return _reply(
                _visual_description_reply(
                    prompt, FORMULA_DESCRIPTION if formula_page else DIAGRAM_DESCRIPTION
                )
            )
        assert "Source text observations:" in prompt
        observations: list[dict[str, Any]] = json.loads(
            prompt.split("Source text observations:\n", 1)[1]
        )
        # The authored layouts are mutually exclusive, but the frames overlap the ruled
        # grid's extent, so the diagram claims its occurrences first.
        in_diagram = (
            [observation for observation in observations if _inside(observation, DIAGRAM_REGION)]
            if diagram_page
            else []
        )
        in_formula: dict[str, list[dict[str, Any]]] = {}
        if formula_page:
            for name, box in FORMULA_REGIONS.items():
                in_formula[name] = [
                    observation
                    for observation in observations
                    if observation not in in_diagram and _inside(observation, box)
                ]
        claimed = [item for owned in in_formula.values() for item in owned]
        in_grid = [
            observation
            for observation in observations
            if observation not in in_diagram
            and observation not in claimed
            and _inside(observation, table_bbox)
        ]
        outside = [
            observation
            for observation in observations
            if observation not in in_grid
            and observation not in in_diagram
            and observation not in claimed
        ]
        regions: list[dict[str, object]] = []
        if in_grid:
            owned = in_grid + (outside if table_caption else [])
            extent = _extent(owned)
            bbox = [
                min(region[0], extent[0]),
                min(region[1], extent[1]),
                max(region[2], extent[2]),
                max(region[3], extent[3]),
            ]
            regions.append(_region("table", "Table", bbox, [str(item["id"]) for item in owned]))
            if table_caption:
                outside = []
        if in_diagram:
            owned = in_diagram + (outside if diagram_caption else [])
            extent = _extent(owned)
            bbox = [
                min(DIAGRAM_REGION[0], extent[0]),
                min(DIAGRAM_REGION[1], extent[1]),
                max(DIAGRAM_REGION[2], extent[2]),
                max(DIAGRAM_REGION[3], extent[3]),
            ]
            regions.append(_region("diagram", "Diagram", bbox, [str(item["id"]) for item in owned]))
            if diagram_caption:
                outside = []
        for name, owned in in_formula.items():
            if not owned:
                continue
            box = FORMULA_REGIONS[name]
            extent = _extent(owned)
            regions.append(
                _region(
                    f"formula-{name}",
                    "Formula",
                    [
                        min(box[0], extent[0]),
                        min(box[1], extent[1]),
                        max(box[2], extent[2]),
                        max(box[3], extent[3]),
                    ],
                    [str(item["id"]) for item in owned],
                )
            )
        if outside:
            regions.append(
                _region("body", "Text", _extent(outside), [str(item["id"]) for item in outside])
            )
        return _reply({"regions": regions, "unassigned_span_ids": [], "diagnostics": []})

    return sender


def _reply(content: dict[str, object]) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}]}
    ).encode()


def ingest_generic_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    filename: str = "meridian-semiannual.pdf",
    label: str = DOCUMENT_LABEL,
    page_count: int = 3,
    output_dir: Path | None = None,
    table_page: bool | TableSpec = False,
    table_caption: bool = False,
    diagram_page: bool = False,
    diagram_caption: bool = False,
    formula_page: bool = False,
    formula_rule: bool = True,
    max_live_calls: int | None = None,
) -> tuple[IngestionSummary, list[bytes]]:
    """Ingest an authored PDF through the semantics stage with one stubbed layout call per page.

    ``table_page`` draws a native ruled table on the last page (``True`` for the default
    grid, or a ``TableSpec``); ``table_caption`` makes the stub layout hand that page's
    caption line to the Table region as well.
    ``diagram_page`` draws two labelled frames joined by an arrow instead, which costs
    two further stubbed calls (the typed and the natural-language visual branch);
    ``diagram_caption`` hands that page's caption line to the Diagram region.
    ``formula_page`` draws two formula regions instead, so four further stubbed calls;
    ``formula_rule=False`` omits the fraction rule, which withholds the proof.
    ``max_live_calls`` overrides the budget so a test can starve the visual branches.
    """
    spec = _spec(table_page)
    for key, value in {
        "OPENAI_API_KEY": "offline-secret",
        "OPENAI_BASE_URL": PROVIDER_BASE_URL,
        "OPENAI_MODEL": "offline-test",
    }.items():
        monkeypatch.setenv(key, value)
    pdf = authored_pdf(
        tmp_path / filename,
        page_count=page_count,
        label=label,
        embedded_font=True,
        table_page=table_page,
        diagram_page=diagram_page,
        diagram_caption=diagram_caption,
        formula_page=formula_page,
        formula_rule=formula_rule,
    )
    calls: list[bytes] = []
    monkeypatch.setattr(
        "ragspine.common.evidence.providers.json_completion._send_once",
        text_partition_sender(
            calls,
            table_caption=table_caption,
            table_bbox=spec.bbox if spec is not None else TABLE_BBOX,
            diagram_page=diagram_page,
            diagram_caption=diagram_caption,
            formula_page=formula_page,
        ),
    )
    visual_calls = 4 if formula_page else (2 if diagram_page else 0)
    summary = ingest_pdf(
        pdf=pdf,
        stage="semantics",
        max_live_calls=page_count + visual_calls if max_live_calls is None else max_live_calls,
        output_dir=output_dir if output_dir is not None else tmp_path / "ingestion",
    )
    return summary, calls


def publish_generic_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    filename: str,
    label: str,
    page_count: int,
    embedder: EmbeddingPort,
    output_dir: Path | None = None,
    table_page: bool | TableSpec = False,
) -> DraftPublication:
    """Ingest, qualify, index with the injected embedder and publish one document."""
    ingest, _ = ingest_generic_semantics(
        tmp_path,
        monkeypatch,
        filename=filename,
        label=label,
        page_count=page_count,
        output_dir=output_dir,
        table_page=table_page,
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
        embedder=embedder,
    )
    return publish_draft(
        source_store=source_store,
        processing_store=processing_store,
        processing_id=indexed.indexed_processing_id,
    )


def resolve_table_member(published: DraftPublication) -> RetrievalContext:
    """Hydrate the single published Table member through the real stores; no model call."""
    sources = LocalDocumentStore(Path(published.source_store), activate_on_publish=False)
    outputs = ProcessingStore(Path(published.processing_store))
    manifest = outputs.load(published.published_processing_id)
    assert manifest.retrieval is not None
    plan, _ = outputs.load_retrieval(manifest.retrieval)
    (member,) = tuple(item for item in plan.members if item.kind is ObjectKind.TABLE)
    hit = PinnedRetrievalHit(plan.snapshot_id, member.member_id, 1.0)
    return resolve_processing_context(sources, outputs, manifest.retrieval, hit)
