"""ragspine 控制台 CLI 的端到端测试（直接调 main([...])，不依赖已安装的入口脚本）。

覆盖三条子命令的核心契约：
- quickstart —— 全程离线、零 key：一条命中（带血缘）+ 一条查不到（坦白拒答、不编造），
  作为首次用户的"会引用真实来源、且绝不臆造"的零依赖演示。
- ask —— 镜像 scripts/ask.py：从 --db 建 FactStore、用 mock provider 跑 answer_question，
  命中打印数值 + 来源，未命中坦白拒答；缺库路径给出诚实报错。
- version —— 打印分发版本号。
- 无参 / 未知选项 —— argparse 以非零退出（不静默吞）。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.cli import main
from ragspine.storage.fact_store import Fact, SqliteFactStore


# ---------------------------------------------------------------------------
# quickstart：headline 离线演示
# ---------------------------------------------------------------------------

def test_quickstart_runs_offline_and_shows_found_with_provenance(capsys):
    """quickstart 退出 0，输出含一个具体命中值及其来源（doc/locator 片段）。"""
    rc = main(["quickstart"])
    assert rc == 0
    out = capsys.readouterr().out
    # 命中值（合成 ACME_CN FY2024 REVENUE 1320 USD_M）必须出现。
    assert "1320" in out
    assert "USD_M" in out
    # 血缘：来源文件名 + 定位串片段必须出现（证明会引用真实来源）。
    assert "ACME_FY2024_Results.pptx" in out
    assert "slide=" in out


def test_quickstart_shows_honest_not_found_refusal(capsys):
    """quickstart 同时演示一条查不到：坦白拒答、绝不提供推测数字。"""
    rc = main(["quickstart"])
    assert rc == 0
    out = capsys.readouterr().out
    # 确定性诚实拒答文案（agent/MockProvider 的 not_found 兜底）。
    assert "查不到" in out
    assert "不提供" in out or "不提供推测数字" in out


# ---------------------------------------------------------------------------
# ask：镜像 scripts/ask.py 的离线问答
# ---------------------------------------------------------------------------

@pytest.fixture()
def tiny_fact_db(tmp_path):
    """建一个仅含一条 ACME_CN FY2024 REVENUE 事实的临时 fact 库，返回路径。"""
    db_path = tmp_path / "facts.db"
    store = SqliteFactStore(db_path)
    store.init_schema()
    store.upsert_facts([
        Fact(
            metric_code="REVENUE", entity="ACME_CN", geography="CN",
            channel="TOTAL", period_type="FY", period="2024",
            value=1320.0, unit="USD_M",
            source_doc_id="ACME_FY2024_Results.pptx",
            source_locator="slide=6,table=1,row=2,col=3",
        )
    ])
    store.close()
    return db_path


def test_ask_found_prints_value_and_source(capsys, tiny_fact_db):
    """ask 命中：mock provider 给出确定值 + 来源血缘。"""
    rc = main([
        "ask", "中国内地FY2024的REVENUE是多少",
        "--db", str(tiny_fact_db), "--provider", "mock",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1320" in out
    assert "ACME_FY2024_Results.pptx" in out


def test_ask_not_found_refuses(capsys, tiny_fact_db):
    """ask 未命中：坦白拒答，不编造数字。"""
    rc = main([
        "ask", "中国内地FY2030的REVENUE是多少",
        "--db", str(tiny_fact_db), "--provider", "mock",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "查不到" in out


def test_ask_missing_db_is_honest_error(capsys, tmp_path):
    """ask 指向不存在的库：诚实报错、非零退出，绝不悄悄返回空答案。"""
    missing = tmp_path / "nope.db"
    rc = main(["ask", "随便问问", "--db", str(missing)])
    assert rc != 0
    err = capsys.readouterr().err
    assert str(missing) in err


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

def test_version_prints_version_string(capsys):
    """version 退出 0，打印一个非空的版本号（形如 X.Y...）。"""
    rc = main(["version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out  # 非空
    # 至少含一个数字段（版本号必有数字）。
    assert any(ch.isdigit() for ch in out)


# ---------------------------------------------------------------------------
# 无参 / 未知选项
# ---------------------------------------------------------------------------

def test_no_args_exits_nonzero(capsys):
    """不带子命令：argparse 报错并非零退出（不静默成功）。"""
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0


def test_unknown_option_exits_nonzero(capsys):
    """未知选项：argparse 以非零码退出。"""
    with pytest.raises(SystemExit) as exc:
        main(["--definitely-invalid"])
    assert exc.value.code != 0
