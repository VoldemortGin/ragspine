"""A mixed synthetic report and an offline model stub that answers every ingest request.

``mixed_pdf`` authors one page per object kind the generic ingest meets — prose, a ruled
table, an unruled table, a bar chart, an image, a formula and a diagram — each under the
same running header. ``mixed_sender`` classifies each page's regions from the observations
the layout prompt lists and answers every semantic request (chart IR / description, visual
IR / description, page metadata) from the view it was shown; ``tasks`` counts the requests
by task so a test can see exactly which calls a mode sends.
"""

import hashlib
import json
import re
import sqlite3
import zlib
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pdfspine
import pytest

from enterprise_pdf_rag.adapters.folder_pipeline import FolderPipelineResult, run_folder_pipeline
from enterprise_pdf_rag.adapters.offline import OfflineDescriptionEmbedder
from enterprise_pdf_rag.adapters.processing_store import ProcessingStore
from enterprise_pdf_rag.answers.prompt import ModelAnswer, ModelClaim
from ragspine.extraction.evidence.document.models import AssetRef
from ragspine.extraction.evidence.page.models import (
    ObjectKind,
    ObjectProcessingRecord,
    ProcessingManifest,
    StageOutcome,
)
from tests.enterprise_pdf_rag.adapters.generic_publication_helpers import PROVIDER_BASE_URL
from tests.enterprise_pdf_rag.adapters.page_metadata_helpers import metadata_reply
from tests.enterprise_pdf_rag.adapters.test_pdf_ingestion import (
    DEFAULT_TABLE,
    DIAGRAM_NODES,
    DIAGRAM_REGION,
    FORMULA_FRACTION_BBOX,
    FORMULA_POWER_BBOX,
    TableSpec,
    _draw_diagram,
    _draw_formula,
    _draw_table,
)
from tests.enterprise_pdf_rag.adapters.test_source_paint import _FontInsertionPage
from tests.enterprise_pdf_rag.answers.fake_llm import answered, declined, scripted_client

RUNNING_HEADER = "Meridian Group Interim Report"
TITLE = "Meridian Group 1H26 results"
# One unruled 3 x 2 grid: no stroke anywhere, so ``find_tables("lines")`` sees no table.
UNRULED_ROWS_TABLE = TableSpec(
    cells={
        (0, 0): "Item",
        (0, 1): "1H26",
        (1, 0): "Net profit",
        (1, 1): "567",
        (2, 0): "Equity",
        (2, 1): "8,910",
    },
    ruled=False,
)
TABLE_REGION = (15.0, 55.0, 225.0, 145.0)
CHART_REGION = (15.0, 48.0, 225.0, 152.0)
IMAGE_RECT = (60.0, 60.0, 180.0, 130.0)
# What the chart prints: a caption whose trailing parenthetical is the unit, two bars, two
# category labels under them and two value labels above them.
CHART_TITLE = "Sales ($m)"
CHART_BARS = {
    "2024": ((40.0, 95.0, 70.0, 132.0), "120"),
    "2025": ((130.0, 80.0, 160.0, 132.0), "150"),
}
PAGE_KINDS = ("text", "ruled", "unruled", "chart", "image", "formula", "diagram")


def _font(page: pdfspine.Page) -> str:
    cast(_FontInsertionPage, page).insert_font(
        fontname="Authored",
        fontbuffer=(Path(__file__).parents[1] / "fixtures/authored-donut-ascii.ttf").read_bytes(),
    )
    return "Authored"


def _draw_chart(page: pdfspine.Page, fontname: str) -> None:
    page.insert_text((20, 60), CHART_TITLE, fontsize=9, fontname=fontname)
    for category, (rect, value) in CHART_BARS.items():
        page.draw_rect(rect, color=None, fill=(0.2, 0.4, 0.8), width=0)
        page.insert_text((rect[0] + 4, rect[1] - 3), value, fontsize=8, fontname=fontname)
        page.insert_text((rect[0] + 2, 145), category, fontsize=8, fontname=fontname)


def mixed_pdf(path: Path, *, kinds: tuple[str, ...] = PAGE_KINDS) -> Path:
    """One 240 x 160 page per kind, every page under the same running header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pdfspine.open() as document:
        for number, kind in enumerate(kinds):
            page = document.new_page(width=240, height=160)
            fontname = _font(page)
            page.insert_text((20, 14), RUNNING_HEADER, fontsize=7, fontname=fontname)
            if kind == "text":
                page.insert_text((20, 40), TITLE, fontsize=12, fontname=fontname)
                page.insert_text(
                    (20, 70), "Revenue grew in Hong Kong.", fontsize=9, fontname=fontname
                )
                page.insert_text(
                    (20, 90), "Net margin was 12% in 1H26.", fontsize=9, fontname=fontname
                )
                continue
            page.insert_text(
                (20, 36), f"Section {number + 1} {kind}", fontsize=11, fontname=fontname
            )
            if kind == "ruled":
                _draw_table(page, fontname, DEFAULT_TABLE)
            elif kind == "unruled":
                _draw_table(page, fontname, UNRULED_ROWS_TABLE)
            elif kind == "chart":
                _draw_chart(page, fontname)
            elif kind == "image":
                page.draw_rect(IMAGE_RECT, color=None, fill=(0.6, 0.6, 0.6), width=0)
            elif kind == "formula":
                _draw_formula(page, fontname)
            elif kind == "diagram":
                _draw_diagram(page, fontname)
        path.write_bytes(document.tobytes())
    return path


def _reply(content: dict[str, Any]) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}]}
    ).encode()


def _inside(observation: dict[str, Any], box: tuple[float, ...]) -> bool:
    bbox = [float(value) for value in observation["bbox"]]
    return box[0] <= bbox[0] and box[1] <= bbox[1] and bbox[2] <= box[2] and bbox[3] <= box[3]


def _extent(owned: list[dict[str, Any]], box: tuple[float, ...]) -> list[float]:
    boxes = [[round(float(value), 6) for value in item["bbox"]] for item in owned]
    return [
        min([box[0], *(item[0] for item in boxes)]),
        min([box[1], *(item[1] for item in boxes)]),
        max([box[2], *(item[2] for item in boxes)]),
        max([box[3], *(item[3] for item in boxes)]),
    ]


def _region(
    region_id: str, kind: str, bbox: list[float], owned: list[dict[str, Any]]
) -> dict[str, object]:
    return {
        "region_id": region_id,
        "kind": kind,
        "bbox": bbox,
        "source_span_ids": [str(item["id"]) for item in owned],
        "context_span_ids": [],
        "list_items": [],
        "list_ordered": None,
        "parent_id": None,
        "interpretation": f"{kind} region",
    }


def _layout(prompt: str) -> dict[str, Any]:
    observations: list[dict[str, Any]] = json.loads(
        prompt.split("Source text observations:\n", 1)[1]
    )
    texts = {str(item["text"]) for item in observations}
    boxes: list[tuple[str, str, tuple[float, ...]]] = []
    if "ROE =" in texts:
        boxes.extend(
            (
                ("formula-fraction", "Formula", FORMULA_FRACTION_BBOX),
                ("formula-power", "Formula", FORMULA_POWER_BBOX),
            )
        )
    elif "Metric" in texts or "Net profit" in texts:
        boxes.append(("table", "Table", TABLE_REGION))
    elif CHART_TITLE in texts:
        boxes.append(("chart", "Chart", CHART_REGION))
    elif any(text.endswith(" image") for text in texts):
        boxes.append(("image", "Image", IMAGE_RECT))
    elif "PLAN" in texts:
        boxes.append(("diagram", "Diagram", DIAGRAM_REGION))
    regions: list[dict[str, object]] = []
    claimed: list[dict[str, Any]] = []
    for region_id, kind, box in boxes:
        owned = [item for item in observations if item not in claimed and _inside(item, box)]
        claimed.extend(owned)
        regions.append(_region(region_id, kind, _extent(owned, box), owned))
    rest = [item for item in observations if item not in claimed]
    if rest:
        regions.append(_region("body", "Text", _extent(rest, (1e9, 1e9, -1e9, -1e9)), rest))
    return {"regions": regions, "unassigned_span_ids": [], "diagnostics": []}


def _view(prompt: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(prompt.rsplit("\n", 1)[-1]))


def _evidence(element_id: str) -> dict[str, object]:
    return {"element_ids": [element_id], "confidence": "high"}


def _chart_ir(prompt: str) -> dict[str, Any]:
    view = _view(prompt)
    ids = {str(item["text"]): str(item["id"]) for item in view["observations"]}
    title = ids[CHART_TITLE]
    return {
        "schema_version": "chart-observations-v1",
        "svg_digest": view["svg_digest"],
        "grammar": "bar",
        "title": {"text": CHART_TITLE, "evidence": _evidence(title)},
        "period": None,
        "axes": [],
        "points": [
            {
                "point_id": f"sales-{category}",
                "series": {"text": "Sales", "evidence": _evidence(title)},
                "category": {"text": category, "evidence": _evidence(ids[category])},
                "unit": {"text": "$m", "evidence": _evidence(title)},
                "value": {"value": value, "kind": "explicit", "evidence": _evidence(ids[value])},
            }
            for category, (_rect, value) in CHART_BARS.items()
        ],
        "marks": [],
        "diagnostics": [],
    }


def _chart_description(prompt: str) -> dict[str, Any]:
    view = _view(prompt)
    ids = {str(item["text"]): str(item["id"]) for item in view["observations"]}
    claims: list[dict[str, Any]] = [
        {
            "text": CHART_TITLE,
            "evidence": _evidence(ids[CHART_TITLE]),
            "series": None,
            "category": None,
            "unit": None,
            "value": None,
            "period": None,
        }
    ]
    claims.extend(
        {
            "text": f"Sales for {category}: {value} $m.",
            "evidence": {"element_ids": [ids[category], ids[value]], "confidence": "high"},
            "series": "Sales",
            "category": category,
            "unit": "$m",
            "value": value,
            "period": None,
        }
        for category, (_rect, value) in CHART_BARS.items()
    )
    return {
        "schema_version": "figure-description-v1",
        "svg_digest": view["svg_digest"],
        "claims": claims,
        "diagnostics": [],
    }


def _diagram_ir(prompt: str) -> dict[str, Any]:
    view = _view(prompt)
    by_text = {str(item["text"]): str(item["id"]) for item in view["observations"]}
    return {
        "schema_version": "diagram-observations-v1",
        "svg_digest": view["svg_digest"],
        "nodes": [
            {
                "node_id": node_id,
                "label": label,
                "bbox": list(rect),
                "evidence": _evidence(by_text[label]),
            }
            for node_id, (rect, label, _origin) in DIAGRAM_NODES.items()
        ],
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


def _formula_ir(prompt: str) -> dict[str, Any]:
    view = _view(prompt)
    return {
        "schema_version": "formula-observations-v1",
        "svg_digest": view["svg_digest"],
        "source_literal_element_ids": [str(item["id"]) for item in view["observations"]],
        "normalization_state": "inferred",
        "latex": "ROE = \\frac{Net profit}{Equity}",
        "confidence": "medium",
        "diagnostics": [],
    }


def _image_ir(prompt: str) -> dict[str, Any]:
    return {
        "schema_version": "image-observations-v1",
        "svg_digest": _view(prompt)["svg_digest"],
        "visible_objects": [
            {"text": "A grey rectangle", "evidence": {"element_ids": [], "confidence": "low"}}
        ],
        "observed_label_element_ids": [],
        "confidence": "low",
        "diagnostics": [],
    }


def _visual_description(prompt: str) -> dict[str, Any]:
    view = _view(prompt)
    return {
        "schema_version": "visual-description-v1",
        "svg_digest": view["svg_digest"],
        "text": "A plain shape.",
        "evidence": {
            "element_ids": [str(item["id"]) for item in view["observations"]],
            "confidence": "0.5",
        },
        "diagnostics": [],
    }


_VISUAL_TASKS = {
    "Return diagram-observations-v1": ("visual-ir-diagram", _diagram_ir),
    "Return formula-observations-v1": ("visual-ir-formula", _formula_ir),
    "Return image-observations-v1": ("visual-ir-image", _image_ir),
}


def mixed_sender(tasks: Counter[str]) -> Callable[..., bytes]:
    """Answer every ingest request offline; ``tasks`` counts each request by task."""

    def sender(url: str, *, api_key: str, payload: bytes, timeout: float) -> bytes:
        assert url == f"{PROVIDER_BASE_URL}/v1/chat/completions"
        messages = json.loads(payload)["messages"]
        content = messages[1]["content"]
        if isinstance(content, str) and "routing note" in messages[0]["content"]:
            tasks["tree-summary"] += 1
            return _reply({"summary": "Where the report states its figures.", "key_topics": []})
        if isinstance(content, str):
            assert content.startswith("Describe what this one printed page is about")
            tasks["page-metadata"] += 1
            return _reply(metadata_reply(content, fabricate=False))
        prompt = str(content[0]["text"])
        if "Source text observations:" in prompt:
            tasks["page-layout"] += 1
            return _reply(_layout(prompt))
        if "Return chart-observations-v1" in prompt:
            tasks["chart-ir"] += 1
            return _reply(_chart_ir(prompt))
        if "Return figure-description-v1" in prompt:
            tasks["chart-description"] += 1
            return _reply(_chart_description(prompt))
        for marker, (task, reply) in _VISUAL_TASKS.items():
            if marker in prompt:
                tasks[task] += 1
                return _reply(reply(prompt))
        if "Return visual-description-v1" in prompt:
            texts = {str(item["text"]) for item in _view(prompt)["observations"]}
            kind = "diagram" if "PLAN" in texts else "formula" if texts else "image"
            tasks[f"visual-description-{kind}"] += 1
            return _reply(_visual_description(prompt))
        raise AssertionError("unexpected ingest request")

    return sender


# ---- folder runs shared by the lite tests ------------------------------------------------

LLM_ENV = {
    "APP_LLM_API_KEY": "offline-secret",
    "APP_LLM_BASE_URL": PROVIDER_BASE_URL,
    "APP_LLM_MODEL": "offline-test",
}
# Recorded from the release before lite existed (fd302f8), on the mixed seven-page report:
# every file the full-mode folder run writes under the ingestion root (model-cache
# ``contexts/`` records carry a wall-clock ``created_at`` and are compared by name only),
# the published processing id and every model request fingerprint. Full mode must keep
# producing exactly these bytes. Re-recorded for ADR 0029 Amendment 1 (envelopes inline in
# their stage-cache pointers): the 140 envelope objects are gone and the 140 pointers carry
# them, the other 535 files are byte-identical (was ddade1cd…, 815 files; pinned in
# test_inline_stage_cache by turning the pointers back into that form). Re-recorded for
# Amendment 2 (small stage outputs inline after the envelope): the 132 output objects are gone
# and their pointers carry them (was 620f220d…, 675 files; pinned in test_inline_stage_artifacts
# by writing the outputs back as objects).
FULL_STORE_DIGEST = "363650ace4500a9f245ffd7c4b9c152bc15b2c04f793da263ac419ce10244a25"
FULL_STORE_FILES = 543
FULL_PUBLISHED_ID = "7d90791cc842875e334fadecf96072c4e31b626540461967d237f88c915f2e40"
FULL_REQUESTS_DIGEST = "e51e5226da9421778b274054212f07a8d66551179cf72d861c5b1410db0834a5"
FULL_TASKS = {
    "page-layout": 7,
    "page-metadata": 7,
    "chart-ir": 1,
    "chart-description": 1,
    "visual-ir-image": 1,
    "visual-ir-formula": 2,
    "visual-ir-diagram": 1,
    "visual-description-image": 1,
    "visual-description-formula": 2,
    "visual-description-diagram": 1,
}


def lite_env(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    for key, value in LLM_ENV.items():
        monkeypatch.setenv(key, value)
    tasks: Counter[str] = Counter()
    monkeypatch.setattr(
        "ragspine.common.evidence.providers.json_completion._send_once", mixed_sender(tasks)
    )
    # Each model-cache record stores the call's wall time (``diagnostics.elapsed_ms``, rounded);
    # the offline sender answers in well under a millisecond, but a loaded machine can round it
    # to 1 and change the pinned store digest. A fixed clock keeps every record byte stable.
    monkeypatch.setattr("ragspine.common.evidence.providers.json_completion.monotonic", lambda: 0.0)
    return tasks


def mixed_folder(tmp_path: Path) -> Path:
    mixed_pdf(tmp_path / "pdfs" / "mixed.pdf")
    return tmp_path / "pdfs"


# ADR 0029 moved hash-named files from ``<dir>/<name>`` to ``<dir>-sharded/<name[:2]>/<name>``.
# The pinned digest is over each file's logical (flat) name, so it still proves that full mode
# writes the very same files with the very same bytes; ``sharded_layout_only`` pins the move.
# ADR 0036 moved small entries into one ``store.sqlite`` per store root and the model cache
# into ``model-cache.sqlite``: ``store_digest`` maps each db row back to the very same logical
# name and the very same bytes (envelope lines, trailing newlines, inline outputs included), so
# ``FULL_STORE_DIGEST`` is pinned to one value whatever the backend.
_SHARDED = re.compile(r"(?P<dir>[^/]+)-sharded/[0-9a-f]{2}/(?P<name>[^/]+)$")
# The backend db and its side files never enter the logical view themselves.
_DB_ARTIFACTS = re.compile(r"(^|/)(store|model-cache)\.sqlite(-wal|-shm|\.writer.*|\.corrupt-.*)?$")


def _logical(relative: str) -> str:
    return _SHARDED.sub(lambda match: f"{match['dir']}/{match['name']}", relative)


def sharded_layout_only(root: Path) -> bool:
    """No file sits in a legacy flat ``objects/sha256`` or ``stage-cache`` directory (under
    the sqlite backend equally: db rows aside, externals only ever land sharded)."""
    return not any(root.glob("*/*/objects/sha256")) and not any(root.glob("*/*/stage-cache"))


def _decode_row(blob: bytes, encoding: str) -> bytes:
    return zlib.decompress(blob) if encoding == "zlib" else bytes(blob)


def _db_logical_files(db: Path) -> dict[str, bytes]:
    """One store db's rows as the byte-identical file layout they stand for (ADR 0036)."""
    out: dict[str, bytes] = {}
    with closing(sqlite3.connect(db)) as connection:
        for digest, encoding, external, blob in connection.execute(
            "SELECT digest, encoding, external, bytes FROM objects"
        ):
            if external:
                continue  # 外置对象本来就是文件,rglob 会看到它
            out[f"objects/sha256/{digest}"] = _decode_row(blob, str(encoding))
        for fingerprint, envelope_digest, envelope, product, product_encoding in connection.execute(
            "SELECT fingerprint, envelope_digest, envelope, product, product_encoding"
            " FROM stage_cache"
        ):
            payload = str(envelope_digest).encode() + b"\n" + bytes(envelope) + b"\n"
            if product is not None:
                payload += _decode_row(product, str(product_encoding))
            out[f"stage-cache/{fingerprint}"] = payload
        for name, digest in connection.execute("SELECT name, digest FROM pointers"):
            out[str(name)] = str(digest).encode() + b"\n"
        for name, blob in connection.execute("SELECT name, bytes FROM records"):
            out[str(name)] = bytes(blob)
    return out


def _model_cache_db_files(db: Path) -> dict[str, bytes]:
    """One model-cache db's rows as the flat ``requests`` / ``responses`` / ``contexts`` files
    they stand for (sqlite object store PR-3: the record bytes are identical in both backends)."""
    out: dict[str, bytes] = {}
    with closing(sqlite3.connect(db)) as connection:
        for key, record in connection.execute("SELECT record_key, record FROM requests"):
            out[f"requests/{key}.json"] = bytes(record)
        for digest, encoding, blob in connection.execute(
            "SELECT digest, encoding, bytes FROM responses"
        ):
            out[f"responses/{digest}.json"] = _decode_row(blob, str(encoding))
        for fingerprint, encoding, blob in connection.execute(
            "SELECT request_fingerprint, encoding, bytes FROM contexts"
        ):
            out[f"contexts/{fingerprint}.json"] = _decode_row(blob, str(encoding))
    return out


def store_digest(root: Path) -> tuple[str, int, str]:
    files: dict[str, str] = {}
    requests: list[str] = []

    def take(relative: str, source: Path | bytes) -> None:
        if "/verification-receipts/" in relative or relative.startswith("verification-receipts/"):
            # ADR 0034: a receipt records file stats (mtime / ctime), not content.
            return
        if "/model-cache/requests/" in relative:
            requests.append(relative.rsplit("/", 1)[-1].removesuffix(".json"))
        if "/model-cache/contexts/" in relative:
            files[relative] = ""
            return
        data = source if isinstance(source, bytes) else source.read_bytes()
        files[relative] = hashlib.sha256(data).hexdigest()

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if _DB_ARTIFACTS.search(relative):
            expand = {
                "store.sqlite": _db_logical_files,
                "model-cache.sqlite": _model_cache_db_files,
            }
            if path.name in expand:
                prefix = path.parent.relative_to(root).as_posix()
                for name, data in sorted(expand[path.name](path).items()):
                    logical = f"{prefix}/{name}" if prefix != "." else name
                    take(logical, data)
            continue
        take(_logical(relative), path)
    digest = hashlib.sha256(
        json.dumps({key: value for key, value in files.items() if value}, sort_keys=True).encode()
    ).hexdigest()
    return digest, len(files), hashlib.sha256("\n".join(sorted(requests)).encode()).hexdigest()


LITE_TASKS = {
    "page-layout": 7,
    "chart-ir": 1,
    "visual-ir-diagram": 1,
    "visual-description-diagram": 1,
}
SKIPPED_CALLS = {"image": 2, "formula": 4, "chart_description": 1, "page_metadata": 7}
# Each question names the verbatim text its claim must quote and the claim kind it cites.
QUESTIONS = (
    ("chart", "What were Sales in 2025?", "150", "chart_value"),
    ("unruled", "What was Net profit in 1H26?", "567", "quote"),
    ("ruled", "What is the Revenue value?", "1,234", "cell"),
    ("period", "What was the net margin in 1H26?", "Net margin was 12% in 1H26.", "quote"),
    ("formula", "How is ROE defined?", "ROE", "formula"),
)
_PATH_PREFIX = {
    "chart_value": "points.",
    "quote": "fragments.",
    "cell": "cells.",
    "formula": "tokens.",
}


def claim_script(prompt: str) -> ModelAnswer:
    """Cite the first block line that prints the asked needle, under the asked claim kind."""
    question = prompt.split("\n", 2)[1]
    _, _, needle, kind = next(item for item in QUESTIONS if item[1] == question)
    prefix = _PATH_PREFIX[kind]
    for block in prompt.split("| member ")[1:]:
        member_id = block[:64]
        for line in block.splitlines()[1:]:
            # The needle is looked for in the printed text, never in the field path's hash.
            if not line.startswith(prefix) or needle not in line.split(": ", 1)[-1]:
                continue
            path = line.split(" ", 1)[0].removesuffix(":")
            text = line.split(": ", 1)[1].split("  (", 1)[0] if kind == "formula" else needle
            claim = ModelClaim.model_validate(
                {
                    "claim_id": "c1",
                    "member_id": member_id,
                    "kind": kind,
                    "field_path": path,
                    "text": text,
                }
            )
            return answered(f"The report prints {text}.", claim)
    return declined()


def write_questions(tmp_path: Path) -> Path:
    path = tmp_path / "questions.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"id": case, "question": question, "doc": "mixed.pdf", "expected": needle})
            for case, question, needle, _ in QUESTIONS
        )
        + "\n"
    )
    return path


def run_mode(
    tmp_path: Path,
    mode: str | None,
    *,
    questions: Path | None = None,
    build_tree: bool | None = False,
    root: str = "ingestion",
) -> FolderPipelineResult:
    llm, _ = scripted_client(tmp_path / f"answers-{root}-{mode}", claim_script, max_live_calls=20)
    options: dict[str, object] = {}
    if mode is not None:
        options["ingest_mode"] = mode
    if build_tree is not None:
        options["build_tree"] = build_tree
    return run_folder_pipeline(
        tmp_path / "pdfs",
        questions=questions,
        ingestion_root=tmp_path / root,
        max_live_calls_per_pdf=200,
        embedder=OfflineDescriptionEmbedder(),
        answer_llm=llm,
        **options,  # type: ignore[arg-type]
    )


def published_manifest(result: FolderPipelineResult) -> ProcessingManifest:
    (document,) = result.documents
    assert document.ingestion is not None and document.publication is not None
    return ProcessingStore(Path(document.ingestion.processing_store)).load(
        document.publication.published_processing_id
    )


def records_of(manifest: ProcessingManifest, kind: ObjectKind) -> list[ObjectProcessingRecord]:
    return [record for page in manifest.pages for record in page.objects if record.kind is kind]


def stages_of(record: ObjectProcessingRecord) -> dict[str, StageOutcome]:
    return {stage.stage: stage for stage in record.stages}


def processing_store_of(tmp_path: Path, root: str) -> Path:
    (store,) = (tmp_path / root).glob("*/processing")
    return store


def artifact_of(stage: StageOutcome) -> AssetRef:
    assert stage.artifact is not None
    return stage.artifact
