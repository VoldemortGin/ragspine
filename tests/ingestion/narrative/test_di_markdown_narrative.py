"""DI markdown（.md）叙事通道 + 按段切块（页码溯源）测试。

只验证对外行为：
    - extract_di_markdown_narrative：每页每节（页 + heading_path）一个 segment，locator='page={物理页序}'，
      表格线性化为「行标题 | 列标题: 值」、图 caption 在前、页眉页脚不进正文；普通 markdown 整份 = 1 页。
    - ingest_narrative：.md 总是按段切块（chunk locator 带 page=N、heading 为标题路径、seq/chunk_id
      全文档统一重排）；segment_chunking 开关让旧后缀也按段切块（默认关，旧路径字节不变见
      test_legacy_chunk_snapshot.py）。
    - RESTRICTED 定级在 .md 路径同样生效；session / service / CLI 接线。
"""

import os
from dataclasses import replace

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.common.company_profile import CompanyProfile, load_company_profile
from ragspine.common.sensitivity import SensitivityPolicy
from ragspine.ingestion.narrative.narrative_extract import (
    SUPPORTED_SUFFIXES,
    extract_di_markdown_narrative,
    extract_narrative,
)
from ragspine.ingestion.narrative.narrative_ingest import ingest_narrative
from ragspine.retrieval.chunking.chunk_store import ChunkStore

_DI_MD = """<!-- PageHeader="Confidential header" -->
# Overview

Intro paragraph line one
line two

<!-- PageFooter="Footer text" -->
<!-- PageNumber="7" -->
<!-- PageBreak -->

## Results

<table><caption>Key metrics</caption>
<tr><th rowspan="2">Market ($m)</th><th colspan="2">1H26</th></tr>
<tr><th>VONB</th><th>ANP</th></tr>
<tr><td>Alpha</td><td>10</td><td>20</td></tr>
<tr><td>Beta</td><td>30</td><td></td></tr>
</table>

<table><tr><td>plain a</td><td>plain b</td></tr></table>

# Outlook

<figure>
<figcaption>Chart A</figcaption>
Bar one 40
Bar two 60
</figure>
"""


def _write(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def store(tmp_path):
    s = ChunkStore(tmp_path / "chunks.db")
    s.init_schema()
    yield s
    s.close()


# ===========================================================================
# 抽取
# ===========================================================================


def test_md_is_supported_and_dispatched(tmp_path):
    assert ".md" in SUPPORTED_SUFFIXES
    doc = extract_narrative(_write(tmp_path, "deck.md", _DI_MD))
    assert doc.doc_id == "deck.md"
    assert doc.file_hash
    assert doc.segments


def test_segments_grouped_by_page_and_heading_path(tmp_path):
    doc = extract_di_markdown_narrative(_write(tmp_path, "deck.md", _DI_MD))
    got = [(s.source_locator, s.heading_path) for s in doc.segments]
    # 物理页序：第 1 页的 PageNumber="7" 不改变 locator。
    assert got == [
        ("page=1", ("Overview",)),
        ("page=2", ("Overview", "Results")),
        ("page=2", ("Outlook",)),
    ]
    assert doc.segments[0].text == "Overview\nIntro paragraph line one\nline two"
    assert "Confidential header" not in doc.to_text()
    assert "Footer text" not in doc.to_text()


def test_table_linearized_rows_with_column_headers(tmp_path):
    doc = extract_di_markdown_narrative(_write(tmp_path, "deck.md", _DI_MD))
    lines = doc.segments[1].text.split("\n")
    assert lines == [
        "Results",
        "Key metrics",
        "Market ($m)",
        "Alpha | 1H26 / VONB: 10",
        "Alpha | 1H26 / ANP: 20",
        "Beta | 1H26 / VONB: 30",
        "plain a | plain b",
    ]


def test_figure_caption_first(tmp_path):
    doc = extract_di_markdown_narrative(_write(tmp_path, "deck.md", _DI_MD))
    assert doc.segments[2].text == "Outlook\nChart A\nBar one 40\nBar two 60"


def test_plain_markdown_is_one_page_with_headings(tmp_path):
    md = "Preface text.\n\n# Title\n\nBody one.\n\n## Sub\n\nBody two.\n"
    doc = extract_narrative(_write(tmp_path, "readme.md", md))
    assert [(s.source_locator, s.heading_path, s.text) for s in doc.segments] == [
        ("page=1", (), "Preface text."),
        ("page=1", ("Title",), "Title\nBody one."),
        ("page=1", ("Title", "Sub"), "Sub\nBody two."),
    ]


def test_empty_markdown_has_no_segments(tmp_path):
    doc = extract_narrative(_write(tmp_path, "empty.md", "\n\n<!-- PageBreak -->\n"))
    assert doc.segments == []


# ===========================================================================
# 入库：.md 按段切块 → page 溯源
# ===========================================================================


def test_md_ingest_chunks_carry_page_locator_and_heading(tmp_path, store):
    p = _write(tmp_path, "deck.md", _DI_MD)
    report = ingest_narrative([p], store)
    assert report.files[0].status == "ingested"
    chunks = store.iter_chunks(doc_id="deck.md")
    assert [c.source_locator for c in chunks] == [
        "deck.md@page=1#para1-3",
        "deck.md@page=2#para1-7",
        "deck.md@page=2#para1-4",
    ]
    assert [c.heading for c in chunks] == ["Overview", "Overview > Results", "Outlook"]
    assert [c.seq for c in chunks] == [0, 1, 2]
    assert [c.chunk_id for c in chunks] == ["deck.md#c0", "deck.md#c1", "deck.md#c2"]
    assert report.files[0].n_chunks == 3


def test_segment_chunking_renumbers_across_segments_and_is_stable(tmp_path, store):
    body = "\n\n".join(f"# S{i}\n\n" + ("word " * 60).strip() for i in range(3))
    p = _write(tmp_path, "long.md", body)
    ingest_narrative([p], store, max_chars=120, overlap_chars=0)
    first = [(c.chunk_id, c.source_locator, c.text) for c in store.iter_chunks(doc_id="long.md")]
    assert len(first) > 3
    assert [cid for cid, _, _ in first] == [f"long.md#c{i}" for i in range(len(first))]
    assert len({cid for cid, _, _ in first}) == len(first)
    assert all(loc.startswith("long.md@page=1#para") for _, loc, _ in first)

    p.write_text(body + "\n", encoding="utf-8")  # hash 变化 → 重入库
    ingest_narrative([p], store, max_chars=120, overlap_chars=0)
    again = [(c.chunk_id, c.source_locator, c.text) for c in store.iter_chunks(doc_id="long.md")]
    assert again == first


def test_segment_chunking_switch_on_for_legacy_suffix(tmp_path, store):
    p = _write(tmp_path, "notes.txt", "First block.\n\nSecond block.")
    ingest_narrative([p], store, segment_chunking=True)
    chunks = store.iter_chunks(doc_id="notes.txt")
    assert [c.source_locator for c in chunks] == [
        "notes.txt@para=1#para1",
        "notes.txt@para=2#para1",
    ]
    assert [c.chunk_id for c in chunks] == ["notes.txt#c0", "notes.txt#c1"]
    assert [c.heading for c in chunks] == ["", ""]


def test_segment_chunking_with_injected_chunker_keeps_parent_ids_distinct(tmp_path, store):
    from ragspine.retrieval.chunking.chunker import make_chunker

    p = _write(tmp_path, "deck.md", _DI_MD)
    ingest_narrative([p], store, chunker=make_chunker("parent_child"))
    chunks = store.iter_chunks(doc_id="deck.md")
    assert all("@page=" in c.source_locator for c in chunks)
    assert all(c.parent_locator.startswith("deck.md@page=") for c in chunks)
    by_segment = {}
    for c in chunks:
        by_segment.setdefault(c.source_locator.split("#")[0] + c.heading, set()).add(c.parent_id)
    parents = [pid for pids in by_segment.values() for pid in pids]
    assert len(parents) == len(set(parents))


# ===========================================================================
# RESTRICTED 定级在 .md 路径生效
# ===========================================================================


def _profile(policy: SensitivityPolicy) -> CompanyProfile:
    return replace(load_company_profile("definitely-not-a-real-path.toml"), sensitivity=policy)


def test_md_restricted_keyword_classified(tmp_path, store, monkeypatch):
    import ragspine.ingestion.narrative.narrative_ingest as ni

    policy = SensitivityPolicy(restricted_keywords=["remuneration"])
    monkeypatch.setattr(ni, "_PROFILE", _profile(policy))
    leaky = _write(tmp_path, "board.md", "# Pay\n\n<!-- PageBreak -->\n\nExec remuneration table.")
    normal = _write(tmp_path, "deck.md", _DI_MD)
    ingest_narrative([leaky, normal], store)
    assert {c.sensitivity for c in store.iter_chunks(doc_id="board.md")} == {"RESTRICTED"}
    assert {c.sensitivity for c in store.iter_chunks(doc_id="deck.md")} == {"INTERNAL"}


def test_md_restricted_chunks_never_egress(tmp_path, store, monkeypatch):
    import ragspine.ingestion.narrative.narrative_ingest as ni
    from ragspine.retrieval.lexical.retrieval import NarrativeIndex
    from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever

    monkeypatch.setattr(
        ni, "_PROFILE", _profile(SensitivityPolicy(restricted_filename_patterns=["board"]))
    )
    ingest_narrative(
        [
            _write(tmp_path, "board.md", "# Alpha VONB\n\nAlpha VONB SECRET_TOKEN rose."),
            _write(tmp_path, "deck.md", _DI_MD),
        ],
        store,
    )
    snippets = NarrativeIndexRetriever(NarrativeIndex(store)).retrieve("Alpha VONB")
    assert snippets
    assert all(s["doc_id"] != "board.md" for s in snippets)
    assert all("SECRET_TOKEN" not in s["text"] for s in snippets)


# ===========================================================================
# 接线：session / service config / job / CLI
# ===========================================================================


def test_session_routes_md_to_narrative_only(tmp_path):
    from ragspine.session import _NARRATIVE_SUFFIXES, _STRUCTURED_SUFFIXES

    assert ".md" in _NARRATIVE_SUFFIXES
    assert ".md" not in _STRUCTURED_SUFFIXES


def test_service_config_switch_from_env():
    from ragspine.service.config import ServiceConfig

    assert ServiceConfig(db_path="x.db").narrative_segment_chunking is False
    cfg = ServiceConfig.from_env({"RAGSPINE_NARRATIVE_SEGMENT_CHUNKING": "true"})
    assert cfg.narrative_segment_chunking is True


def test_job_payload_switch_reaches_ingest(tmp_path):
    from ragspine.service.tasks.jobs import run_narrative_ingest_job

    p = _write(tmp_path, "notes.txt", "First block.\n\nSecond block.")
    db = tmp_path / "chunks.db"
    run_narrative_ingest_job(
        {"inputs": [str(p)], "chunk_db_path": str(db), "segment_chunking": True}
    )
    s = ChunkStore(db)
    try:
        assert [c.source_locator for c in s.iter_chunks()] == [
            "notes.txt@para=1#para1",
            "notes.txt@para=2#para1",
        ]
    finally:
        s.close()


def test_cli_segment_chunking_flag(tmp_path):
    from ragspine.cli.ingest_narrative import main

    p = _write(tmp_path, "notes.txt", "First block.\n\nSecond block.")
    db = tmp_path / "chunks.db"
    assert main([str(p), "--db", str(db), "--segment-chunking"]) == 0
    s = ChunkStore(db)
    try:
        assert s.iter_chunks()[0].source_locator == "notes.txt@para=1#para1"
    finally:
        s.close()
