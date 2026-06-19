"""上手宪法 rule 5（ADR 0012）：所有「会打开 fact 库」的 shell 入口，缺库必须大声报错，
绝不静默建空库再返回假「查不到」（那是一种披着 not-found 外衣的编造）。

ENTRY_POINTS 是这类入口的注册表——将来新增第三个入口时往这里加一行，本测试自动覆盖它，
把 rule 5 钉成一类约束而非两个一次性补丁。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.cli.main import main as cli_main
from scripts.ask import main as ask_main


def _ask_py(db: object) -> int:
    return ask_main(["--provider", "mock", "--db", str(db), "x"])


def _cli_ask(db: object) -> int:
    return cli_main(["ask", "x", "--db", str(db)])


ENTRY_POINTS = [("scripts/ask.py", _ask_py), ("ragspine ask", _cli_ask)]


@pytest.mark.parametrize("name,entry", ENTRY_POINTS, ids=[e[0] for e in ENTRY_POINTS])
def test_missing_db_errors_and_does_not_silently_create(name, entry, tmp_path, capsys):
    missing = tmp_path / "nonexistent.db"
    rc = entry(missing)
    assert rc != 0, f"{name}：缺库应返回非零退出码"
    assert not missing.exists(), f"{name}：不得静默建空库"
    err = capsys.readouterr().err
    assert "不存在" in err or str(missing) in err, f"{name}：应给出指向缺失库的明确错误"
