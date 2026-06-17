"""冻结金标：反编造的期间剥离正则（ADR 0004 pin-before-touch，P0 不变量）。

detect_fabricated_numbers 先用这个正则把【合法期间 token】(FY2024 / 2024H1 / 2025Q1 …)
从答案里剥掉，再判断是否仍残留未授权数字 = 编造。ADR 0004 计划把这个 pattern 改为从
temporal 维派生——本测试钉住 pattern 逐字不变：放宽它 = 把更多 token 当"合法期间"剥掉
= 漏判编造数字 = 削弱反编造。这是 P0 红线，迁移绝不能动它。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.eval.qa_eval import _PERIOD_TOKEN_RE


def test_period_token_regex_frozen():
    assert _PERIOD_TOKEN_RE.pattern == (
        r"(?:FY\s*)?(?:19|20)\d{2}\s*年?\s*(?:H\s*[12]|Q\s*[1-4]|上半年|下半年)?"
    )
