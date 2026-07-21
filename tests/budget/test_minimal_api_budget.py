"""上手宪法 rule 3（ADR 0012）：包根 __all__ 恰好是 4 个「最小可用 API」名字。

放宽这一组 = 首答要认的概念变多 = 违背「少抽象、快上手」。这 4 个名字也必须能从
包根惰性取到，且就是其源模块里的那个对象。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

import ragspine

EXPECTED = ("RAGSpine", "FactStore", "Fact", "MockProvider", "answer_question")


def test_root_all_is_exactly_the_minimal_four():
    assert ragspine.__all__ == EXPECTED


def test_each_minimal_name_resolves_to_its_source_symbol():
    from ragspine.agent.agent import answer_question
    from ragspine.agent.llm_provider import MockProvider
    from ragspine.storage.fact_store import Fact, FactStore

    assert ragspine.FactStore is FactStore
    assert ragspine.Fact is Fact
    assert ragspine.MockProvider is MockProvider
    assert ragspine.answer_question is answer_question
