"""结构化入库生产 CLI（scripts/ingest.py）的红色测试（TDD red 阶段，任务 3B）。

补结构化入库的真实生产调用方：3A 已加 ingest_file 多格式分发，但无 CLI 入口；
本批新建 scripts/ingest.py（装配 store/registry/queue → 调 ingest_file → 打印
IngestReport 摘要），并顺手修 3A 遗留的 manifest failed 标记（不支持后缀 / PDF
不可读时 record_input 应传 failed=True，使台账失败计数准确）。

只验证外部行为：
- T7 CLI：对 xlsx fixture + --valid-as-of 2025-12-31 → 入库 facts 的 valid_as_of
  均为该值；--dry-run → store 零写入；--db 缺失（目录不存在）时妥善建库。
- T8 manifest failed：ingest_file 对 .txt（不支持后缀）→ manifest 该输入标记
  failed=True + error，失败计数准确。

红色预期（绿阶段才实现）：
- T7：scripts/ingest.py 尚不存在 → import / 子进程 FAIL；其断言依赖时效列也未落地。
- T8：ingest_file 在不支持后缀分支调 _record_manifest 时漏传 failed=True →
  manifest.n_failed == 0、failures 为空，断言 FAIL（3A 遗留 bug）。

每个用例 docstring 带中文 user story。
"""

import os
import subprocess
import sys

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.fixtures.excel import (
    GT_PATH,
    XLSX_PATH,
    main as make_excel_fixtures,
)
from ragspine.extraction.color.color_semantics import MappingRegistry
from ragspine.storage.fact_store import FactStore
from ragspine.ingestion.structured.ingestion_manifest import ManifestStore
from ragspine.ingestion.structured.ingestion import ingest_file
from ragspine.ingestion.review.review_queue import ReviewQueue

PY = sys.executable
INGEST_CLI = str(ROOT_DIR / "scripts" / "ingest.py")


@pytest.fixture(scope="module", autouse=True)
def _ensure_excel_fixture() -> None:
    """确保 excel fixture 存在（缺失则一键再生），CLI 子进程要吃它。"""
    if not XLSX_PATH.exists() or not GT_PATH.exists():
        make_excel_fixtures()


def _run_cli(args: list[str]):
    """从 ROOT_DIR 跑 scripts/ingest.py（真实生产调用路径），返回 CompletedProcess。"""
    return subprocess.run(
        [PY, INGEST_CLI, *args],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
    )


# ===========================================================================
# T7 — 生产 CLI：xlsx fixture + --valid-as-of / --dry-run / --db 自动建库
# ===========================================================================
def test_t7_cli_ingests_xlsx_with_valid_as_of(tmp_path):
    """user story：作为接手方，我用生产 CLI 入库一份 xlsx 并指定本批生效日
    (--valid-as-of 2025-12-31)，入库后每条 fact 的 valid_as_of 都应为该值，
    数字通路从此能回答「这个数截至哪天」。"""
    db = tmp_path / "x.db"
    mdb = tmp_path / "m.db"
    qdb = tmp_path / "q.db"
    proc = _run_cli([
        str(XLSX_PATH),
        "--db", str(db),
        "--mapping-db", str(mdb),
        "--queue-db", str(qdb),
        "--valid-as-of", "2025-12-31",
    ])
    assert proc.returncode == 0, proc.stderr

    fs = FactStore(str(db))
    try:
        rows = fs.execute_read("SELECT valid_as_of FROM fact_metric")
        assert len(rows) >= 1
        assert all(r["valid_as_of"] == "2025-12-31" for r in rows)
    finally:
        fs.close()


def test_t7_cli_prints_report_summary(tmp_path):
    """user story：CLI 跑完应打印 IngestReport 摘要（至少含入库事实数 / 状态），
    让我在终端一眼看到本批结果。"""
    db = tmp_path / "x.db"
    mdb = tmp_path / "m.db"
    qdb = tmp_path / "q.db"
    proc = _run_cli([
        str(XLSX_PATH),
        "--db", str(db),
        "--mapping-db", str(mdb),
        "--queue-db", str(qdb),
        "--valid-as-of", "2025-12-31",
    ])
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # 报告摘要应体现入库条数与状态（具体措辞不约定，至少能看到数字与 ok）。
    assert "ok" in out.lower()
    assert "12" in out  # excel fixture 应入库 12 条事实


def test_t7_cli_dry_run_writes_nothing(tmp_path):
    """user story：--dry-run 预览只产报告、绝不碰库——CLI 跑完 fact_store 零写入。"""
    db = tmp_path / "x.db"
    mdb = tmp_path / "m.db"
    qdb = tmp_path / "q.db"
    proc = _run_cli([
        str(XLSX_PATH),
        "--db", str(db),
        "--mapping-db", str(mdb),
        "--queue-db", str(qdb),
        "--valid-as-of", "2025-12-31",
        "--dry-run",
    ])
    assert proc.returncode == 0, proc.stderr

    # 库可能被建出（init_schema），但事实表必须零写入。
    fs = FactStore(str(db))
    try:
        assert fs.count() == 0
    finally:
        fs.close()


def test_t7_cli_creates_db_when_missing(tmp_path):
    """user story：--db 指向尚不存在的库时，CLI 应妥善建库（init_schema）后入库，
    而非因「库不存在」报错——开箱即用。"""
    db = tmp_path / "nested" / "fresh.db"   # 目录与文件都还不存在
    mdb = tmp_path / "nested" / "m.db"
    qdb = tmp_path / "nested" / "q.db"
    proc = _run_cli([
        str(XLSX_PATH),
        "--db", str(db),
        "--mapping-db", str(mdb),
        "--queue-db", str(qdb),
    ])
    assert proc.returncode == 0, proc.stderr
    assert db.exists()
    fs = FactStore(str(db))
    try:
        assert fs.count() >= 1
    finally:
        fs.close()


def test_t7_cli_main_callable_in_process(tmp_path):
    """user story：CLI 提供可在进程内调用的 main(argv)->int 入口（与
    ingest_narrative.py 同款），便于自动化与测试；成功返回 0。"""
    from ragspine.cli.ingest import main as ingest_main

    db = tmp_path / "x.db"
    mdb = tmp_path / "m.db"
    qdb = tmp_path / "q.db"
    rc = ingest_main([
        str(XLSX_PATH),
        "--db", str(db),
        "--mapping-db", str(mdb),
        "--queue-db", str(qdb),
        "--valid-as-of", "2025-12-31",
    ])
    assert rc == 0
    fs = FactStore(str(db))
    try:
        rows = fs.execute_read("SELECT valid_as_of FROM fact_metric")
        assert rows and all(r["valid_as_of"] == "2025-12-31" for r in rows)
    finally:
        fs.close()


# ===========================================================================
# T8 — manifest failed 标记修复（3A 遗留）：不支持后缀 → failed=True + error
# ===========================================================================
def _init_three(store, registry, queue) -> None:
    store.init_schema()
    queue.init_schema()
    registry.init_schema()


def test_t8_unsupported_suffix_marks_manifest_failed(tmp_path):
    """user story：作为运营，当一份不支持的文件（.txt）入库失败时，批次台账应把
    它计为失败项（n_failed +1、failures 含该 path 与 error），失败计数才准确——
    修 3A 遗留：ingest_file 不支持后缀分支调 record_input 时漏传 failed=True。"""
    store = FactStore(str(tmp_path / "facts.db"))
    registry = MappingRegistry(str(tmp_path / "registry.db"))
    queue = ReviewQueue(str(tmp_path / "queue.db"))
    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    try:
        _init_three(store, registry, queue)
        manifest.init_schema()
        batch_id = manifest.open_batch()

        bad = tmp_path / "notes.txt"
        bad.write_text("这不是一个可抽取的表格文件", encoding="utf-8")
        report = ingest_file(
            bad, store, registry, queue, manifest=manifest, batch_id=batch_id
        )
        assert report.status == "failed"

        rec = manifest.get_batch(batch_id)
        assert rec is not None
        assert rec.n_failed == 1
        assert len(rec.failures) == 1
        assert rec.failures[0]["path"].endswith("notes.txt")
        assert rec.failures[0]["error"]
    finally:
        store.close()
        registry.close()
        queue.close()
        manifest.close()


def test_t8_failed_input_not_counted_as_success(tmp_path):
    """user story：失败的输入不得被混入成功统计——台账里该批 n_facts 不因失败文件
    增长（失败文件零事实），且失败明细 error 指向不支持的格式。"""
    store = FactStore(str(tmp_path / "facts.db"))
    registry = MappingRegistry(str(tmp_path / "registry.db"))
    queue = ReviewQueue(str(tmp_path / "queue.db"))
    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    try:
        _init_three(store, registry, queue)
        manifest.init_schema()
        batch_id = manifest.open_batch()

        bad = tmp_path / "notes.txt"
        bad.write_text("plain text, no table", encoding="utf-8")
        ingest_file(
            bad, store, registry, queue, manifest=manifest, batch_id=batch_id
        )
        rec = manifest.get_batch(batch_id)
        assert rec.n_facts == 0
        assert rec.n_failed == 1
    finally:
        store.close()
        registry.close()
        queue.close()
        manifest.close()
