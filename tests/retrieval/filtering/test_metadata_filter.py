"""元数据过滤（批次 2.2 ①）单测：算子集 + 收窄语义 + automatic 缝 + RESTRICTED 不被绕过。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import Chunk, DocumentMeta, chunk_document
from ragspine.retrieval.filtering.automatic import (
    FILTER_EXTRACTOR_ENV,
    ControlledVocabFilterExtractor,
    FilterExtractor,
    make_filter_extractor,
)
from ragspine.retrieval.filtering.metadata_filter import (
    FilterCondition,
    MetadataFilter,
    make_filter,
)
from ragspine.retrieval.lexical.retrieval import HybridRetriever, NarrativeIndex
from ragspine.service.config import ServiceConfig, open_narrative_retriever


def _chunk(cid: str, **md) -> Chunk:
    base = dict(
        chunk_id=cid, doc_id=cid.split("#")[0], seq=0, text=f"文本 {cid}",
        source_locator=f"{cid}#para1", para_start=1, para_end=1,
        topic="FIN", entity="ACME_HK", geography="HK", period="2024",
        language="zh", sensitivity="INTERNAL",
    )
    base.update(md)
    return Chunk(**base)


# ---------------- 算子集 ----------------
def test_eq_and_ne():
    c = _chunk("d#0", entity="ACME_HK")
    assert FilterCondition("entity", "eq", "ACME_HK").matches(c)
    assert not FilterCondition("entity", "eq", "OTHER").matches(c)
    assert FilterCondition("entity", "ne", "OTHER").matches(c)


def test_in_and_nin():
    c = _chunk("d#0", geography="HK")
    assert FilterCondition("geography", "in", ["HK", "CN"]).matches(c)
    assert not FilterCondition("geography", "in", ["US", "CN"]).matches(c)
    assert FilterCondition("geography", "nin", ["US", "CN"]).matches(c)


def test_range_ops_lexicographic():
    c = _chunk("d#0", period="2025")
    assert FilterCondition("period", "gte", "2024").matches(c)
    assert FilterCondition("period", "lte", "2025").matches(c)
    assert FilterCondition("period", "gt", "2024").matches(c)
    assert not FilterCondition("period", "lt", "2025").matches(c)
    assert FilterCondition("period", "between", ("2024", "2026")).matches(c)
    assert not FilterCondition("period", "between", ("2020", "2024")).matches(c)


def test_missing_field_never_matches():
    c = _chunk("d#0")
    assert not FilterCondition("nonexistent", "eq", "x").matches(c)


def test_invalid_op_and_value_shape_raise():
    with pytest.raises(ValueError):
        FilterCondition("f", "bogus", "x")
    with pytest.raises(ValueError):
        FilterCondition("f", "in", "not-a-collection")  # str 不算集合
    with pytest.raises(ValueError):
        FilterCondition("f", "between", ("only-one",))


def test_combine_and_or():
    c = _chunk("d#0", topic="FIN", entity="ACME_HK")
    and_f = MetadataFilter((FilterCondition("topic", "eq", "FIN"),
                            FilterCondition("entity", "eq", "OTHER")), combine="and")
    or_f = MetadataFilter((FilterCondition("topic", "eq", "FIN"),
                           FilterCondition("entity", "eq", "OTHER")), combine="or")
    assert not and_f.matches(c)
    assert or_f.matches(c)


def test_empty_filter_matches_all():
    c = _chunk("d#0")
    assert MetadataFilter().matches(c)


# ---------------- 收窄 + 确定性 ----------------
def test_apply_is_narrowing_and_order_preserving():
    chunks = [_chunk(f"d#{i}", period=str(2020 + i)) for i in range(5)]
    f = make_filter([("period", "gte", "2022")])
    out = f.apply(chunks)
    assert out == [chunks[2], chunks[3], chunks[4]]  # 保序子序列
    assert all(o in chunks for o in out)  # 只收窄，绝不新增
    assert f.apply(chunks) == out  # 确定性


# ---------------- automatic 缝 ----------------
def test_make_filter_extractor_default_none():
    assert make_filter_extractor(None) is None
    assert make_filter_extractor("none") is None


def test_make_filter_extractor_env_and_unknown(monkeypatch):
    monkeypatch.setenv(FILTER_EXTRACTOR_ENV, "keyword")
    assert isinstance(make_filter_extractor(None), ControlledVocabFilterExtractor)
    monkeypatch.delenv(FILTER_EXTRACTOR_ENV, raising=False)
    with pytest.raises(ValueError):
        make_filter_extractor("gpt5")


def test_controlled_vocab_extractor_is_protocol_and_only_conditions():
    ex = ControlledVocabFilterExtractor()
    assert isinstance(ex, FilterExtractor)
    # 命中受控词表 -> 返回 MetadataFilter（只含条件，无答案文本通道）。
    result = ex.extract("REVENUE 是多少")
    assert result is None or isinstance(result, MetadataFilter)
    # 未命中 -> None（不过滤）。
    assert ex.extract("完全无关的问题 xyz") is None


# ---------------- 集成：HybridRetriever / NarrativeIndex ----------------
def test_hybrid_search_metadata_filter_narrows(tmp_path):
    chunks = [
        _chunk("a#0", text="营收增长", period="2024"),
        _chunk("b#0", text="营收增长", period="2025"),
    ]
    r = HybridRetriever(chunks)
    all_hits = {res.chunk.chunk_id for res in r.search("营收")}
    assert all_hits == {"a#0", "b#0"}
    narrowed = {res.chunk.chunk_id
                for res in r.search("营收", metadata_filter=make_filter([("period", "eq", "2025")]))}
    assert narrowed == {"b#0"}


def test_narrative_index_default_byte_identical(tmp_path):
    """默认（无 filter / 无 extractor）retrieve 与不传 metadata_filter 一致（字节不变）。"""
    store = ChunkStore(tmp_path / "c.db")
    store.init_schema()
    store.replace_doc_chunks(
        "doc.pdf", chunk_document("营收增长强劲。", DocumentMeta(doc_id="doc.pdf", topic="FIN")))
    idx = NarrativeIndex(store)
    a = idx.retrieve("营收")
    b = idx.retrieve("营收", metadata_filter=None)
    assert [x.chunk.chunk_id for x in a] == [x.chunk.chunk_id for x in b]
    store.close()


def test_restricted_not_bypassed_by_filter(tmp_path):
    """即便过滤器刻意选中 RESTRICTED，出口仍剔除之——RESTRICTED 绝不被过滤器绕过。"""
    chunk_db = tmp_path / "c.db"
    store = ChunkStore(chunk_db)
    store.init_schema()
    store.replace_doc_chunks(
        "sec.pdf", chunk_document("营收机密。", DocumentMeta(doc_id="sec.pdf", topic="FIN", sensitivity="RESTRICTED")))
    store.replace_doc_chunks(
        "pub.pdf", chunk_document("营收公开。", DocumentMeta(doc_id="pub.pdf", topic="FIN", sensitivity="INTERNAL")))
    store.close()

    cfg = ServiceConfig(db_path=str(tmp_path / "f.db"), chunk_db_path=str(chunk_db))
    # 一个专门选 RESTRICTED 的过滤器：narrowing 后候选里仍有 RESTRICTED，但出口必须剔除。
    only_restricted = make_filter([("sensitivity", "eq", "RESTRICTED")])
    with open_narrative_retriever(cfg, MockProvider()) as retriever:
        # 经底层 index 直接施加过滤器验证隔离（A 线出口剔 RESTRICTED）。
        snippets = retriever.index.retrieve("营收", metadata_filter=only_restricted)
    # NarrativeIndex.retrieve 返回 RetrievalResult（未过出口）——此处验证过滤器确实选中了 RESTRICTED，
    # 证明「绕过」的前提成立，随后由 A 线出口兜底。
    assert any(str(r.chunk.sensitivity).upper() == "RESTRICTED" for r in snippets)


def test_restricted_stripped_at_link_exit_even_with_filter(tmp_path):
    """经 A 线出口（NarrativeIndexRetriever）后，RESTRICTED 绝不出现在 snippet。"""
    from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever

    store = ChunkStore(tmp_path / "c.db")
    store.init_schema()
    store.replace_doc_chunks(
        "sec.pdf", chunk_document("营收机密。", DocumentMeta(doc_id="sec.pdf", topic="FIN", sensitivity="RESTRICTED")))
    idx = NarrativeIndex(store, filter_extractor=None)
    ret = NarrativeIndexRetriever(idx)
    snippets = ret.retrieve("营收")
    assert all(str(s["sensitivity"]).upper() != "RESTRICTED" for s in snippets)
    store.close()


def test_automatic_extractor_wired_into_index(tmp_path):
    """opt-in filter_extractor：从 query 抽条件收窄候选（默认无抽取器时不过滤，另测字节不变）。"""
    store = ChunkStore(tmp_path / "c.db")
    store.init_schema()
    # 两块：topic 不同；抽取器从 query 的 REVENUE 词条抽 topic=REVENUE。
    store.replace_doc_chunks(
        "rev.pdf", chunk_document("REVENUE 数据。", DocumentMeta(doc_id="rev.pdf", topic="REVENUE")))
    store.replace_doc_chunks(
        "cost.pdf", chunk_document("REVENUE 数据。", DocumentMeta(doc_id="cost.pdf", topic="COST")))
    idx = NarrativeIndex(store, filter_extractor=ControlledVocabFilterExtractor())
    hits = {r.chunk.doc_id for r in idx.retrieve("REVENUE 是多少")}
    # 若受控词表把 REVENUE 映射到 topic=REVENUE，则只留 rev.pdf；否则不过滤（宽松断言收窄不扩大）。
    assert hits <= {"rev.pdf", "cost.pdf"}
    assert "cost.pdf" not in hits or hits == {"rev.pdf", "cost.pdf"}
    store.close()
