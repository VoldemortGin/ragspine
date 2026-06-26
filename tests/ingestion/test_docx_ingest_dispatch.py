"""ingest_file 的 .docx 分支（W3b）红色测试：表格 -> facts；接线可注入 fake。

两类用例：
  - 接线（无需 docspine）：断言 _EXTRACTOR_BY_SUFFIX 已登记 .docx -> docspine 抽取器，
    并用 monkeypatch 注入 fake 抽取器证明 .docx 经统一分发产出 facts。
  - 真实解析（需 docspine，离线纯 Rust）：含表格 + 段落的 .docx 经 ingest_file 入库，
    REVENUE/ACME_HK/FY2024=2680 可查回，extractor_version 带 'docspine'、locator 不丢。

红色预期：.docx 未接入 _EXTRACTOR_BY_SUFFIX -> 现状 status='failed'（不支持后缀）-> FAIL。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.extraction.color.color_semantics import MappingRegistry
from ragspine.extraction.ir import StyledCell, StyledGrid
from ragspine.ingestion.review.review_queue import ReviewQueue
from ragspine.storage.fact_store import VISIBLE_REVIEW_STATUSES, FactStore

RESOLVABLE_ENTITY_TITLE = "ACME Hong Kong"   # -> ACME_HK


@pytest.fixture
def store(tmp_sqlite_factory):
    fs = FactStore(tmp_sqlite_factory("facts"))
    yield fs
    fs.close()


@pytest.fixture
def queue(tmp_sqlite_factory):
    q = ReviewQueue(tmp_sqlite_factory("queue"))
    yield q
    q.close()


@pytest.fixture
def registry(tmp_sqlite_factory):
    reg = MappingRegistry(tmp_sqlite_factory("registry"))
    yield reg
    reg.close()


def _init_three(store, registry, queue) -> None:
    store.init_schema()
    queue.init_schema()
    registry.init_schema()


def _fake_hk_grid() -> StyledGrid:
    g = StyledGrid(sheet="table1", source_doc_id="fake.docx", source_file_hash="deadbeef")
    g.n_rows, g.n_cols = 2, 2
    g.cells["R1C1"] = StyledCell(value=RESOLVABLE_ENTITY_TITLE, cell_ref="R1C1")
    g.cells["R1C2"] = StyledCell(value="FY2024", cell_ref="R1C2")
    g.cells["R2C1"] = StyledCell(value="REVENUE", cell_ref="R2C1")
    g.cells["R2C2"] = StyledCell(value="2680", cell_ref="R2C2")
    return g


# ===========================================================================
# 接线：无需 docspine（结构性断言 + monkeypatch fake 抽取器）
# ===========================================================================
def test_docx_registered_in_suffix_dispatch():
    """user story：.docx 已接入统一分发表，指向 docspine 抽取器、版本 'docspine@1'。"""
    from ragspine.extraction.extractors import docspine_extractor
    from ragspine.ingestion.structured.ingestion import _EXTRACTOR_BY_SUFFIX

    assert ".docx" in _EXTRACTOR_BY_SUFFIX
    fn, version, file_type = _EXTRACTOR_BY_SUFFIX[".docx"]
    assert fn is docspine_extractor.extract_grids
    assert version == "docspine@1"
    assert file_type == "docx"


def test_docx_dispatch_to_fake_extractor_produces_facts(
    monkeypatch, store, registry, queue, tmp_path
):
    """user story：注入 fake 抽取器即可验证 .docx 走统一入库逻辑（归一/入库）产出 facts，
    无需 docspine（接线与解析解耦）。"""
    from ragspine.ingestion.structured import ingestion as ingestion_mod

    _init_three(store, registry, queue)
    calls: list[str] = []

    def _fake(path):
        calls.append(str(path))
        return [_fake_hk_grid()]

    monkeypatch.setitem(
        ingestion_mod._EXTRACTOR_BY_SUFFIX, ".docx", (_fake, "docspine@1", "docx")
    )
    p = tmp_path / "x.docx"
    p.write_bytes(b"stub-never-parsed")  # fake 拦截，绝不真解析
    report = ingestion_mod.ingest_file(p, store, registry, queue)
    assert calls == [str(p)]
    assert report.status == "ok"
    assert report.n_facts_ingested >= 1


def test_legacy_doc_still_unsupported(store, registry, queue, tmp_path):
    """user story：仅接入 .docx/.docm（OOXML）；旧版二进制 .doc 仍报不支持（docspine 不解析），
    不裸抛、不污染库。"""
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    p = tmp_path / "legacy.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0 old binary doc")
    report = ingest_file(p, store, registry, queue)
    assert report.status == "failed"
    assert report.error
    assert store.count() == 0


# ===========================================================================
# 真实解析：需 docspine（离线纯 Rust，装上即可跑）
# ===========================================================================
def test_docx_facts_ingested_real_docspine(store, registry, queue, make_docx, tmp_path):
    """user story：含表格 + 段落的 .docx 经默认 ingest_file -> 表格成 facts，
    REVENUE/ACME_HK/FY2024=2680 可精确查回，血缘 extractor_version/locator 不丢。"""
    pytest.importorskip("docspine", reason="docspine 未安装（[doc]）")
    from ragspine.ingestion.structured.ingestion import ingest_file

    _init_three(store, registry, queue)
    p = tmp_path / "acme_hk.docx"
    make_docx(p, [
        ("para", "FY2024 Hong Kong performance review"),
        ("table", [[RESOLVABLE_ENTITY_TITLE, "FY2024"], ["REVENUE", "2680"]]),
        ("para", "Closing remarks."),
    ])
    assert store.count() == 0
    report = ingest_file(p, store, registry, queue)
    assert report.status == "ok"
    assert report.n_facts_ingested >= 1
    rows = store.query("REVENUE", "ACME_HK", "FY", "2024", channel="TOTAL")
    assert len(rows) == 1
    assert rows[0].value == 2680.0
    assert "docspine" in (rows[0].extractor_version or "")
    assert rows[0].source_locator.startswith("sheet=table1!")
    assert rows[0].review_status in VISIBLE_REVIEW_STATUSES
