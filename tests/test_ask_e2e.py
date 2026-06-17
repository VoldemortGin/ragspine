"""CLI 端到端测试：scripts/ask.py 用 mock provider 离线跑通「香港去年REVENUE多少」。

固定参考日期 2026-06-12（去年=FY2025），合成事实写入临时库，断言答案含数值+血缘。
"""

import os
from datetime import date

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from scripts.ask import main as ask_main
from ragspine.storage.fact_store import Fact, FactStore


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "fact_metric.db"
    fs = FactStore(db_path)
    fs.init_schema()
    fs.upsert_facts([
        Fact(
            metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="TOTAL",
            period_type="FY", period="2025", value=1702.0, unit="USD_M",
            source_doc_id="ACME_FY2025_Results.pptx",
            source_locator="slide=5,table=1,row=2,col=3",
        ),
    ])
    fs.close()
    return db_path


def test_ask_e2e_found(seeded_db, capsys):
    """「香港去年REVENUE多少」mock 全链路：意图→澄清→tool use 循环→确定值+血缘。"""
    rc = ask_main([
        "--provider", "mock",
        "--db", str(seeded_db),
        "--reference-date", "2026-06-12",
        "香港去年REVENUE多少",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1702" in out
    assert "ACME_FY2025_Results.pptx" in out
    assert "slide=5,table=1,row=2,col=3" in out


def test_ask_e2e_not_found_never_fabricates(seeded_db, capsys):
    rc = ask_main([
        "--provider", "mock",
        "--db", str(seeded_db),
        "--reference-date", "2026-06-12",
        "中国去年ROE多少",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "查不到" in out
    assert "1702" not in out


def test_ask_e2e_mock_needs_no_api_key(seeded_db, capsys, monkeypatch):
    """mock 模式不依赖任何 API key 环境变量。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = ask_main([
        "--provider", "mock",
        "--db", str(seeded_db),
        "--reference-date", "2026-06-12",
        "香港去年REVENUE多少",
    ])
    assert rc == 0
    assert "1702" in capsys.readouterr().out


def test_ask_e2e_clarification_question(seeded_db, capsys):
    """指标缺失：CLI 输出前置澄清问题而非乱答。"""
    rc = ask_main([
        "--provider", "mock",
        "--db", str(seeded_db),
        "--reference-date", "2026-06-12",
        "香港去年多少",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "REVENUE" in out  # 列出可选指标供收窄
