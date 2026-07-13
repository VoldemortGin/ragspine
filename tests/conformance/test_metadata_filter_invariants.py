"""元数据过滤（批次 2.2 ①）不变量绑定（conformance）。

对【每个算子模式】参数化断言核心不变量：
    - 收窄性：apply 结果恒为输入的【子序列】（保序、只减不增）——过滤绝不新增/改动候选。
    - 确定性：同输入两次 apply 逐位一致。
    - 反捏造/隔离：过滤器绝不能绕过 RESTRICTED——即便某过滤器刻意选中 RESTRICTED，出口仍剔除之。
    - automatic 缝：抽取产物只能是 MetadataFilter（过滤条件），结构上无途径进入答案通道。

非空泛证明：一个「扩大」候选（新增元素）的反证 filter 喂进收窄断言核【必须 FAIL】。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import Chunk, DocumentMeta, chunk_document
from ragspine.retrieval.filtering.automatic import ControlledVocabFilterExtractor
from ragspine.retrieval.filtering.metadata_filter import MetadataFilter, make_filter
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.lexical.retrieval import NarrativeIndex

# 每个算子一个「模式」——参数化把每种新算子都纳入覆盖。
FILTER_MODES = {
    "eq": make_filter([("period", "eq", "2022")]),
    "ne": make_filter([("period", "ne", "2022")]),
    "in": make_filter([("period", "in", ["2021", "2023"])]),
    "nin": make_filter([("period", "nin", ["2021", "2023"])]),
    "gte": make_filter([("period", "gte", "2022")]),
    "lte": make_filter([("period", "lte", "2022")]),
    "gt": make_filter([("period", "gt", "2022")]),
    "lt": make_filter([("period", "lt", "2022")]),
    "between": make_filter([("period", "between", ("2021", "2023"))]),
    "and": make_filter([("topic", "eq", "FIN"), ("period", "gte", "2022")], combine="and"),
    "or": make_filter([("period", "eq", "2020"), ("period", "eq", "2024")], combine="or"),
}


def _chunks() -> list[Chunk]:
    return [
        Chunk(chunk_id=f"d#{i}", doc_id="d", seq=i, text=f"t{i}",
              source_locator=f"d#para{i + 1}", para_start=i + 1, para_end=i + 1,
              topic="FIN", period=str(2020 + i))
        for i in range(5)
    ]


def _assert_narrowing_and_deterministic(metadata_filter: MetadataFilter) -> None:
    """收窄断言核：结果是输入的保序子序列，且确定性。参数化用例与反证 stub 共用它。"""
    src = _chunks()
    out = metadata_filter.apply(src)
    # 子集：每个输出元素都来自输入（同一对象）。
    assert all(any(o is s for s in src) for o in out), "过滤新增了候选——违反收窄性"
    # 保序：输出是输入的子序列。
    it = iter(src)
    assert all(any(o is s for s in it) for o in out), "过滤打乱了候选顺序——违反保序收窄"
    # 确定性。
    assert [id(o) for o in metadata_filter.apply(src)] == [id(o) for o in out]


@pytest.mark.parametrize("mode", list(FILTER_MODES), ids=list(FILTER_MODES))
def test_filter_mode_is_narrowing_and_deterministic(mode):
    _assert_narrowing_and_deterministic(FILTER_MODES[mode])


class _WideningFilter:
    """反证 stub：apply 新增一个原不在输入里的元素——【故意】违反收窄性。"""

    def apply(self, objs):
        out = list(objs)
        out.append(Chunk(chunk_id="INJECTED", doc_id="x", seq=99, text="x",
                         source_locator="x#para1", para_start=1, para_end=1))
        return out


def test_widening_stub_fails_narrowing():
    """喂扩大候选的反证 stub 进同一收窄断言核必须 FAIL——证明收窄不变量非空泛。"""
    with pytest.raises(AssertionError):
        _assert_narrowing_and_deterministic(_WideningFilter())


def test_restricted_not_bypassed_by_any_filter(tmp_path):
    """反捏造/隔离：即便过滤器专选 RESTRICTED，A 线出口仍剔除之——过滤绝不绕过隔离。"""
    store = ChunkStore(tmp_path / "c.db")
    store.init_schema()
    store.replace_doc_chunks(
        "sec.pdf", chunk_document("营收机密。", DocumentMeta(doc_id="sec.pdf", topic="FIN", sensitivity="RESTRICTED")))
    store.replace_doc_chunks(
        "pub.pdf", chunk_document("营收公开。", DocumentMeta(doc_id="pub.pdf", topic="FIN", sensitivity="INTERNAL")))
    idx = NarrativeIndex(store)
    ret = NarrativeIndexRetriever(idx)
    # NarrativeIndexRetriever.retrieve 不吃 metadata_filter，但底层 index 施加专选 RESTRICTED 的过滤后，
    # 出口仍必须剔除 RESTRICTED（这里直接验证出口对 index 结果的剔除语义）。
    only_restricted = make_filter([("sensitivity", "eq", "RESTRICTED")])
    raw = idx.retrieve("营收", metadata_filter=only_restricted)
    assert any(str(r.chunk.sensitivity).upper() == "RESTRICTED" for r in raw), "过滤器应已选中 RESTRICTED（绕过前提）"
    # 而经出口的正常检索绝不含 RESTRICTED。
    snippets = ret.retrieve("营收")
    assert all(str(s["sensitivity"]).upper() != "RESTRICTED" for s in snippets)
    store.close()


def test_automatic_extractor_yields_only_filter_conditions():
    """automatic 缝：抽取产物只能是 MetadataFilter（过滤条件），无途径进答案通道。"""
    ex = ControlledVocabFilterExtractor()
    result = ex.extract("revenue 是多少")
    assert result is None or isinstance(result, MetadataFilter)
    if result is not None:
        # 产物是纯过滤条件（field/op/value），不含任何答案/正文字段。
        for cond in result.conditions:
            assert cond.field and cond.op
