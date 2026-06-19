"""上手宪法 rule 2（ADR 0012）：answer_question 的「神圣签名」冻结。

恰好 3 个必填位置参（question/store/provider），其余一律 keyword-only 且带默认值。
新增能力只能走「带默认的 keyword-only」或可选层——第 4 个必填位置参、或去掉某可选的
默认值，都会让本测试变红，挡住把首答上手成本悄悄抬高的改动。
"""

import inspect
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question


def test_answer_question_has_exactly_three_required_positional():
    params = list(inspect.signature(answer_question).parameters.values())
    required_positional = [
        p
        for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
    ]
    assert [p.name for p in required_positional] == ["question", "store", "provider"]


def test_all_other_params_are_keyword_only_with_defaults():
    params = list(inspect.signature(answer_question).parameters.values())
    extras = [p for p in params if p.name not in ("question", "store", "provider")]
    for p in extras:
        assert p.kind == p.KEYWORD_ONLY, f"参数 {p.name} 必须是 keyword-only（不得成为新的必填位置参）"
        assert p.default is not p.empty, f"参数 {p.name} 必须带默认值（首答路径上不得多一个必填项）"
