"""页图开关与入库关联的接线：ServiceConfig（RAGSPINE_PAGE_IMAGES*）、本地 facade、CLI、worker job。"""

import json
import os
from pathlib import Path

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider, split_message_content
from ragspine.ingestion.page_images.source_pdf import SourcePdfError, sidecar_path
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.page_images.attach import PageImageRetriever
from ragspine.retrieval.page_images.store import PageImageStore
from tests.ingestion.page_images.fixtures import make_pdf, write_deck

_PAGES = ["Agency channel mix overview.", "Partnership channel outlook.", "Closing remarks."]


class _ImageRecorder(MockProvider):
    supports_image_input = True

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.seen.append(messages)
        text, _ = split_message_content(messages[-1]["content"])
        return super().chat([*messages[:-1], {"role": "user", "content": text}], tools=tools)


def _tables(db: Path) -> set[str]:
    store = ChunkStore(db)
    try:
        return {
            r[0] for r in store.execute_read("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        store.close()


def test_service_config_defaults_and_env():
    from ragspine.service.config import ServiceConfig

    cfg = ServiceConfig(db_path="x.db")
    assert (cfg.page_images, cfg.page_images_top_n) == ("off", 3)
    assert (cfg.page_image_dpi, cfg.page_image_max_side, cfg.page_image_dir) == (144, 1568, None)
    env = ServiceConfig.from_env(
        {
            "RAGSPINE_PAGE_IMAGES": "on",
            "RAGSPINE_PAGE_IMAGES_TOP_N": "5",
            "RAGSPINE_PAGE_IMAGE_DPI": "200",
            "RAGSPINE_PAGE_IMAGE_MAX_SIDE": "2000",
            "RAGSPINE_PAGE_IMAGE_DIR": "/tmp/imgs",
        }
    )
    assert (env.page_images, env.page_images_top_n) == ("on", 5)
    assert (env.page_image_dpi, env.page_image_max_side, env.page_image_dir) == (
        200,
        2000,
        "/tmp/imgs",
    )


def test_trigger_fields_defaults_env_and_preset():
    from ragspine.service.config import ServiceConfig, make_retrieval_preset

    cfg = ServiceConfig(db_path="x.db")
    assert (cfg.page_images_trigger, cfg.page_images_max) == ("has_table,low_text", None)
    assert (cfg.page_images_low_text_chars, cfg.page_images_figure_min_chars) == (300, 10)
    env = ServiceConfig.from_env(
        {
            "RAGSPINE_PAGE_IMAGES": "tagged",
            "RAGSPINE_PAGE_IMAGES_TRIGGER": "has_figure,low_text",
            "RAGSPINE_PAGE_IMAGES_MAX": "2",
            "RAGSPINE_PAGE_IMAGES_LOW_TEXT_CHARS": "200",
            "RAGSPINE_PAGE_IMAGES_FIGURE_MIN_CHARS": "25",
        }
    )
    assert (env.page_images, env.page_images_trigger, env.page_images_max) == (
        "tagged",
        "has_figure,low_text",
        2,
    )
    assert (env.page_images_low_text_chars, env.page_images_figure_min_chars) == (200, 25)
    with pytest.raises(ValueError):
        ServiceConfig.from_env({"RAGSPINE_PAGE_IMAGES_MAX": "two"})
    preset = make_retrieval_preset(
        page_images="tagged",
        page_images_trigger="any",
        page_images_max=1,
        page_images_low_text_chars=100,
        page_images_figure_min_chars=0,
    )
    assert (preset.page_images, preset.page_images_trigger, preset.page_images_max) == (
        "tagged",
        "any",
        1,
    )
    assert (preset.page_images_low_text_chars, preset.page_images_figure_min_chars) == (100, 0)
    kept = preset.with_overrides(page_images="all")
    assert (kept.page_images_max, kept.page_images_trigger) == (1, "any")


@pytest.mark.parametrize(
    "overrides",
    [
        {"page_images": "maybe"},
        {"page_images": "tagged", "page_images_trigger": "has_chart"},
        {"page_images": "tagged", "page_images_trigger": ""},
        {"page_images": "tagged", "page_images_max": -1},
        {"page_images": "all", "page_images_low_text_chars": -5},
        {"page_images": "all", "page_images_figure_min_chars": -1},
    ],
)
def test_invalid_trigger_values_fail_at_assembly(tmp_path, overrides):
    from ragspine.service.config import ServiceConfig, open_narrative_retriever

    db = str(tmp_path / "k.db")
    cfg = ServiceConfig(db_path=db, chunk_db_path=db, embedding="none", **overrides)
    with pytest.raises(ValueError):
        with open_narrative_retriever(cfg, MockProvider()):
            pass


def test_open_narrative_retriever_wraps_only_when_on(tmp_path):
    from ragspine.service.config import ServiceConfig, open_narrative_retriever

    db = str(tmp_path / "k.db")
    base = ServiceConfig(db_path=db, chunk_db_path=db, embedding="none", page_parent="dedup")
    with open_narrative_retriever(base, MockProvider()) as retriever:
        assert not isinstance(retriever, PageImageRetriever)
    on = ServiceConfig(
        db_path=db,
        chunk_db_path=db,
        embedding="none",
        page_parent="dedup",
        page_images="on",
        page_images_top_n=2,
        page_image_dir=str(tmp_path / "imgs"),
    )
    with open_narrative_retriever(on, MockProvider()) as retriever:
        assert isinstance(retriever, PageImageRetriever)
        assert (retriever.top_n, retriever.page_parent) == (2, "dedup")
        assert Path(retriever.image_dir) == tmp_path / "imgs"
        assert retriever.chunk_db_path == db
    from ragspine.retrieval.page_images.trigger.retriever import PageImageTriggerRetriever

    tagged = ServiceConfig(
        db_path=db,
        chunk_db_path=db,
        embedding="none",
        page_parent="dedup",
        page_images="tagged",
        page_images_top_n=4,
        page_images_trigger="has_figure",
        page_images_figure_min_chars=20,
    )
    with open_narrative_retriever(tagged, MockProvider()) as retriever:
        assert isinstance(retriever, PageImageTriggerRetriever)
        assert (retriever.mode, retriever.max_images, retriever.figure_min_chars) == (
            "tagged",
            4,
            20,
        )
        assert retriever.trigger == frozenset({"has_figure"})
        assert isinstance(retriever.base, PageImageRetriever) and retriever.base.top_n == 4


def test_facade_ingest_links_pdf_and_ask_sends_images(tmp_path):
    from ragspine import RAGSpine
    from ragspine.service.config import make_retrieval_preset

    md, pdf = write_deck(tmp_path, _PAGES)
    ws = tmp_path / "ws"
    result = RAGSpine.local(ws).ingest(md, source_pdf=pdf)
    assert result.page_image_report is not None
    assert result.page_image_report.counts()["rendered"] == 1
    images = PageImageStore(ws / "knowledge.db")
    try:
        rows = images.list_doc("deck.md")
    finally:
        images.close()
    assert [r.page for r in rows] == [1, 2, 3]
    assert all(r.path.is_relative_to(ws / "page_images") for r in rows)

    # 默认 off：provider 只收到字符串
    provider = _ImageRecorder()
    RAGSpine.local(ws, provider=provider, retrieval=make_retrieval_preset(page_parent="dedup")).ask(
        "agency channel mix"
    )
    assert all(isinstance(m[-1]["content"], str) for m in provider.seen)

    provider = _ImageRecorder()
    preset = make_retrieval_preset(page_parent="dedup", page_images="on", page_images_top_n=1)
    rag = RAGSpine.local(ws, provider=provider, retrieval=preset)
    assert (rag._service_config().page_images, rag._service_config().page_images_top_n) == ("on", 1)
    rag.ask("agency channel mix")
    content = provider.seen[-1][-1]["content"]
    assert isinstance(content, list)
    text, parts = split_message_content(content)
    assert [(p["name"], p["doc_id"], p["page"]) for p in parts] == [("p1.png", "deck.md", 1)]
    assert "图：p1.png" in text
    assert Path(parts[0]["path"]).is_file()


def test_facade_rejects_mismatched_pdf_before_writing(tmp_path):
    from ragspine import RAGSpine

    md, _ = write_deck(tmp_path, _PAGES)
    wrong = make_pdf(tmp_path / "wrong.pdf", ["only one page"])
    ws = tmp_path / "ws"
    with pytest.raises(SourcePdfError, match="页数不一致"):
        RAGSpine.local(ws).ingest(md, source_pdf=wrong)
    store = ChunkStore(ws / "knowledge.db")
    try:
        store.init_schema()
        assert store.count() == 0
    finally:
        store.close()


def test_facade_source_pdf_requires_single_markdown(tmp_path):
    from ragspine import RAGSpine

    folder = tmp_path / "in"
    folder.mkdir()
    write_deck(folder, _PAGES)
    (folder / "other.md").write_text("# x\n\ny\n", encoding="utf-8")
    with pytest.raises(SourcePdfError):
        RAGSpine.local(tmp_path / "ws").ingest(folder, source_pdf=folder / "deck.pdf")


def test_facade_without_pdf_is_unchanged(tmp_path):
    from ragspine import RAGSpine

    md, _ = write_deck(tmp_path, _PAGES)
    ws = tmp_path / "ws"
    result = RAGSpine.local(ws).ingest(md)
    assert result.page_image_report is None
    assert not any(t.startswith("page_image") for t in _tables(ws / "knowledge.db"))
    assert not (ws / "page_images").exists()


def test_facade_reads_sidecar(tmp_path):
    from ragspine import RAGSpine

    md, pdf = write_deck(tmp_path, _PAGES)
    sidecar_path(md).write_text(json.dumps({"source_pdf": pdf.name}), encoding="utf-8")
    result = RAGSpine.local(tmp_path / "ws").ingest(md)
    assert result.page_image_report is not None
    assert result.page_image_report.docs[0].n_images == 3


def test_cli_source_pdf(tmp_path, capsys):
    from ragspine.cli.ingest_narrative import main

    md, pdf = write_deck(tmp_path, _PAGES)
    db = tmp_path / "chunks.db"
    rc = main([str(md), "--db", str(db), "--source-pdf", str(pdf), "--page-image-dpi", "72"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "页图" in out
    images = PageImageStore(db)
    try:
        rows = images.list_doc("deck.md")
    finally:
        images.close()
    assert [r.dpi for r in rows] == [72, 72, 72]


def test_cli_mismatch_exits_nonzero(tmp_path, capsys):
    from ragspine.cli.ingest_narrative import main

    md, _ = write_deck(tmp_path, _PAGES)
    wrong = make_pdf(tmp_path / "wrong.pdf", ["x"])
    rc = main([str(md), "--db", str(tmp_path / "c.db"), "--source-pdf", str(wrong)])
    assert rc == 2
    assert "页数不一致" in capsys.readouterr().err


def test_worker_job_with_source_pdf(tmp_path):
    from ragspine.service.tasks.jobs import run_narrative_ingest_job

    md, pdf = write_deck(tmp_path, _PAGES)
    db = str(tmp_path / "chunks.db")
    result = run_narrative_ingest_job(
        {
            "inputs": [str(md)],
            "chunk_db_path": db,
            "source_pdf": str(pdf),
            "page_image_dpi": 72,
            "page_image_max_side": 1568,
            "page_image_dir": None,
        }
    )
    assert result["page_images"]["counts"]["rendered"] == 1
    assert result["page_images"]["docs"][0]["n_images"] == 3
    assert "path" not in json.dumps(result["page_images"])


def test_worker_job_without_pdf_report_unchanged(tmp_path):
    from ragspine.service.tasks.jobs import run_narrative_ingest_job

    md, _ = write_deck(tmp_path, _PAGES)
    result = run_narrative_ingest_job(
        {"inputs": [str(md)], "chunk_db_path": str(tmp_path / "c.db")}
    )
    assert "page_images" not in result


def test_worker_job_mismatch_is_a_validation_error(tmp_path):
    from ragspine.service.tasks.jobs import JobError, run_narrative_ingest_job

    md, _ = write_deck(tmp_path, _PAGES)
    wrong = make_pdf(tmp_path / "wrong.pdf", ["x"])
    with pytest.raises(JobError) as info:
        run_narrative_ingest_job(
            {"inputs": [str(md)], "chunk_db_path": str(tmp_path / "c.db"), "source_pdf": str(wrong)}
        )
    assert info.value.stage == "validation"
