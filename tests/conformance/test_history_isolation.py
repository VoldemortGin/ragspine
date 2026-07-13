"""对话历史入参（ADR 0017）× RESTRICTED 双出口隔离 的参数化 conformance。

绑定不变量：给 answer_question 传对话历史【绝不】给 RESTRICTED 开新出口。对【带历史 / 不带历史】
两形态参数化断言：以真实 NarrativeIndexRetriever（出口已剔 RESTRICTED）驱动叙事路，最终答案与
sources 都绝不含 RESTRICTED 内容——历史只作生成上下文、不改检索，隔离从 base 出口继承、忠实透传。

非空泛证明：一个“泄漏 RESTRICTED”的反证 retriever 喂进同一断言核【必须 FAIL】（含带历史形态）——
证明断言确实能抓到泄漏，隔离不是被历史悄悄绕过的假象。
"""

import os
from datetime import date
from typing import Any

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question
from ragspine.agent.llm_provider import MockProvider
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta, chunk_document
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.storage.fact_store import SqliteFactStore

REF = date(2026, 6, 12)

RESTRICTED_MARK = "机密竞争细节"

# 带历史 / 不带历史 两形态：历史里【故意】塞 RESTRICTED 字样，验证它既不进检索、也不成为证据。
HISTORY_FORMS = {
    "no_history": None,
    "with_history": [("user", f"上一轮聊到{RESTRICTED_MARK}"),
                     ("assistant", f"{RESTRICTED_MARK}属于受限内容。")],
}


@pytest.fixture
def store(tmp_db_path):
    fs = SqliteFactStore(tmp_db_path)
    fs.init_schema()
    yield fs
    fs.close()


def _real_retriever(tmp_path) -> NarrativeIndexRetriever:
    cs = ChunkStore(tmp_path / "chunks.db")
    cs.init_schema()
    cs.replace_doc_chunks("pub.pdf", chunk_document(
        "行业竞争态势加剧，价格战拖累利润。",
        DocumentMeta(doc_id="pub.pdf", topic="FIN", sensitivity="INTERNAL")))
    cs.replace_doc_chunks("sec.pdf", chunk_document(
        f"行业竞争{RESTRICTED_MARK}：内部渠道数据。",
        DocumentMeta(doc_id="sec.pdf", topic="FIN", sensitivity="RESTRICTED")))
    return NarrativeIndexRetriever(NarrativeIndex(cs))


def _assert_no_restricted(result) -> None:
    assert RESTRICTED_MARK not in result.answer, f"RESTRICTED 出域到答案：{result.answer!r}"
    assert RESTRICTED_MARK not in result.answer_plain
    for s in result.sources:
        assert "sec.pdf" not in str(s.get("doc", "")), f"provenance 指向 RESTRICTED 文档：{s!r}"


@pytest.mark.parametrize("history", list(HISTORY_FORMS.values()), ids=list(HISTORY_FORMS))
def test_restricted_isolation_holds_with_and_without_history(store, tmp_path, history):
    retriever = _real_retriever(tmp_path)
    result = answer_question("行业竞争态势怎么样", store, MockProvider(reference_date=REF),
                             reference_date=REF, narrative_retriever=retriever, history=history)
    # 公开块应被检索到（叙事路真跑通），RESTRICTED 块绝不出域。
    assert "价格战" in result.answer
    _assert_no_restricted(result)


class _LeakyRetriever:
    """反证 retriever：直吐一个 RESTRICTED snippet（【故意】不剔除）。"""

    def retrieve(self, query: str, *, filters: dict[str, str] | None = None,
                 top_k: int = 50) -> list[dict[str, Any]]:
        return [{"text": f"泄漏的{RESTRICTED_MARK}", "doc_id": "sec.pdf",
                 "locator": "sec.pdf#p1", "sensitivity": "RESTRICTED"}]


@pytest.mark.parametrize("history", list(HISTORY_FORMS.values()), ids=list(HISTORY_FORMS))
def test_leaky_retriever_fails_isolation_core(store, history):
    """泄漏 RESTRICTED 的反证 retriever 喂进同一断言核必须 FAIL——两形态都非空泛。"""
    result = answer_question("行业竞争态势怎么样", store, MockProvider(reference_date=REF),
                             reference_date=REF, narrative_retriever=_LeakyRetriever(),
                             history=history)
    with pytest.raises(AssertionError):
        _assert_no_restricted(result)
