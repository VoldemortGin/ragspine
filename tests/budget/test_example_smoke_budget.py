"""上手宪法：examples/minimal_rag.py 必须可离线跑通，且同时演示命中(带血缘) + 坦白拒答。

既防示例代码腐烂（它是「4 个名字 = 全部最小 API」的活证明），也兼作反编造 + 公共 API
可运行的烟囱测：FOUND 行带数值与来源、缺失问题走确定性拒答。
"""

import os
import subprocess
import sys

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)


def test_minimal_rag_example_runs_and_shows_found_and_refusal():
    example = ROOT_DIR / "examples" / "minimal_rag.py"
    proc = subprocess.run(
        [sys.executable, str(example)], capture_output=True, text=True, cwd=str(ROOT_DIR)
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "1320 USD_M" in out  # 命中：数值
    assert "ACME_FY2024_Results.pptx" in out  # 命中：来源血缘
    assert "查不到" in out  # 缺失 → 坦白拒答（绝不臆造）
