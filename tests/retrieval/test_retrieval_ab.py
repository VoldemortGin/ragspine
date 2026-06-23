"""BM25-vs-hybrid A/B 检索 harness 测试（TDD 红色阶段，GAP-A 向量通道）。

User story（中文）：
    作为接手 PoC 的 GIO 算法工程师，在把向量通道升为「默认可接线」后，我需要一个
    **确定性、可复现**的 A/B harness：给定一份 gold（每行 {query, relevant_chunk_ids}）
    与一份块语料，分别用 BM25-only（embedding=None）和 hybrid（注入某后端）跑同一套
    NarrativeIndex 检索，算出并对比 Recall@k / MRR，打印对照表。我先用一份**小型合成
    gold**（构造成 gold 可被词法命中）证明 harness 算得对、跑得通；真实 recall 对比需
    真实样本标注 + 真模型（待 GPU infra）。

诚实边界（钉死在测试意图里）：
    合成 gold 只证明 harness 正确性，不代表任何真实检索质量；hybrid 用确定性词法散列
    后端时与 BM25 高度相关，不应、也不被断言带来语义增益。

覆盖：
    A1 指标正确性（compute_recall_at_k / compute_mrr 手算例）。
    A1 harness 跑通（run_ab 对合成 gold 同时产出 BM25-only 与 hybrid 的 Recall@k/MRR，
        数值落在 [0,1]，BM25-only 在该合成集上命中预期，结果确定可复现）。
    fixture（data/golden/retrieval_ab_sample.jsonl 存在、可被 load_ab_gold 解析、字段齐备）。
    CLI（scripts/eval_retrieval_ab.py main 对合成 gold 实跑、退出码 0、打印对照表，
        表中同时含 bm25 与 hybrid 两列）。

红色预期：scripts/eval_retrieval_ab.py 与 data/golden/retrieval_ab_sample.jsonl 尚不存在，
import / 文件读取 / 调用因此 FAIL。
"""

import json
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend
from ragspine.retrieval.lexical.retrieval import NarrativeIndex

from ragspine.cli.eval_retrieval_ab import (
    AB_GOLD_PATH,
    AbGoldCase,
    _build_corpus_chunk_db,
    compute_mrr,
    compute_recall_at_k,
    load_ab_gold,
    main as ab_main,
    run_ab,
)


# ===========================================================================
# A1 指标手算例：Recall@k / MRR
# ===========================================================================

def test_recall_at_k_hand_example():
    """Recall@k：retrieved 前 k 命中 relevant 的占比。

    relevant={a,b,c}、retrieved=[a, x, b, y]；k=2 命中 {a} -> 1/3；k=4 命中 {a,b} -> 2/3。
    """
    relevant = ["a", "b", "c"]
    retrieved = ["a", "x", "b", "y"]
    assert compute_recall_at_k(retrieved, relevant, k=2) == pytest.approx(1 / 3)
    assert compute_recall_at_k(retrieved, relevant, k=4) == pytest.approx(2 / 3)


def test_recall_at_k_full_hit():
    """全部 relevant 都在前 k 内 -> Recall@k == 1.0。"""
    assert compute_recall_at_k(["a", "b"], ["a", "b"], k=5) == pytest.approx(1.0)


def test_recall_at_k_no_hit():
    """无命中 -> 0.0；空 relevant 约定为 1.0（无可召回项即满分，不污染均值）。"""
    assert compute_recall_at_k(["x", "y"], ["a"], k=2) == pytest.approx(0.0)
    assert compute_recall_at_k(["x"], [], k=2) == pytest.approx(1.0)


def test_mrr_hand_example():
    """MRR：第一个命中 relevant 的名次倒数（rank 从 1 起）。

    retrieved=[x, a, b]、relevant={a} -> 首个命中在 rank 2 -> 1/2。
    """
    assert compute_mrr(["x", "a", "b"], ["a"]) == pytest.approx(0.5)


def test_mrr_first_position_and_miss():
    """首位命中 -> 1.0；完全未命中 -> 0.0。"""
    assert compute_mrr(["a", "b"], ["a"]) == pytest.approx(1.0)
    assert compute_mrr(["x", "y"], ["a"]) == pytest.approx(0.0)


# ===========================================================================
# 合成 KB + gold（就地构造，确定性）
# ===========================================================================

def _meta(doc_id: str, **overrides) -> DocumentMeta:
    kwargs = dict(
        doc_id=doc_id, title=doc_id, topic="FIN", entity="ACME_HK",
        geography="HK", period="2025", language="zh", sensitivity="INTERNAL",
    )
    kwargs.update(overrides)
    return DocumentMeta(**kwargs)


# 构造成「query 词法可命中对应文档」的小语料：每篇单段 -> chunk_id == doc_id#c0。
_AB_DOCS = (
    ("HK_REVENUE.pptx", "香港 REVENUE 营收持续增长，银保渠道表现稳健。"),
    ("CN_REVENUE.pptx", "中国 REVENUE 增长由代理人产能提升与银保渠道扩张驱动。"),
    ("HK_REG.pptx", "香港监管动态：MPFA 强积金新规要求披露管理费。"),
    ("OTHER.pptx", "weekend cricket match report 板球比赛与本业务无关。"),
)

_AB_GOLD = (
    {"query": "香港 REVENUE 增长", "relevant_chunk_ids": ["HK_REVENUE.pptx#c0"]},
    {"query": "MPFA 强积金 新规", "relevant_chunk_ids": ["HK_REG.pptx#c0"]},
)


@pytest.fixture
def ab_chunk_db(tmp_path):
    """就地建合成块库：每篇文档单段、chunk_id 形如 '<doc>#c0'。"""
    db = tmp_path / "ab_chunks.db"
    store = ChunkStore(db)
    store.init_schema()
    index = NarrativeIndex(store)
    for doc_id, text in _AB_DOCS:
        index.ingest(text, _meta(doc_id))
    store.close()
    return db


@pytest.fixture
def ab_gold_cases():
    return [AbGoldCase(query=g["query"], relevant_chunk_ids=g["relevant_chunk_ids"])
            for g in _AB_GOLD]


# ===========================================================================
# A1 harness 跑通：run_ab 产出 BM25-only 与 hybrid 两套指标
# ===========================================================================

def test_run_ab_produces_both_arms(ab_chunk_db, ab_gold_cases):
    """run_ab 同时产出 BM25-only 与 hybrid 两套 Recall@k/MRR，数值在 [0,1]。"""
    report = run_ab(
        ab_chunk_db, ab_gold_cases,
        embedding_backend=DeterministicEmbeddingBackend(), k=5,
    )
    assert {"bm25", "hybrid"} <= set(report)
    for arm in ("bm25", "hybrid"):
        rec = report[arm]["recall_at_k"]
        mrr = report[arm]["mrr"]
        assert 0.0 <= rec <= 1.0
        assert 0.0 <= mrr <= 1.0


def test_run_ab_bm25_hits_expected_on_synthetic(ab_chunk_db, ab_gold_cases):
    """合成 gold 构造成可被词法命中：BM25-only Recall@k 命中预期（应为满分 1.0）。"""
    report = run_ab(ab_chunk_db, ab_gold_cases, embedding_backend=None, k=5)
    assert report["bm25"]["recall_at_k"] == pytest.approx(1.0)
    assert report["bm25"]["mrr"] == pytest.approx(1.0)


def test_run_ab_deterministic_reproducible(ab_chunk_db, ab_gold_cases):
    """同一输入两次跑 run_ab 结果完全一致（确定性、可复现）。"""
    r1 = run_ab(ab_chunk_db, ab_gold_cases,
                embedding_backend=DeterministicEmbeddingBackend(), k=5)
    r2 = run_ab(ab_chunk_db, ab_gold_cases,
                embedding_backend=DeterministicEmbeddingBackend(), k=5)
    assert r1 == r2


def test_run_ab_hybrid_does_not_crash_and_keeps_bm25_recall(ab_chunk_db, ab_gold_cases):
    """注入确定性后端的 hybrid 臂不崩，且在该合成集上 recall 不低于 BM25-only。

    诚实边界：确定性后端是词法散列、非语义，这里只断言「不破坏」，不主张语义增益。
    """
    report = run_ab(ab_chunk_db, ab_gold_cases,
                    embedding_backend=DeterministicEmbeddingBackend(), k=5)
    assert report["hybrid"]["recall_at_k"] >= report["bm25"]["recall_at_k"]


# ===========================================================================
# fixture：data/golden/retrieval_ab_sample.jsonl
# ===========================================================================

def test_ab_gold_fixture_exists_and_parses():
    """随附合成 gold fixture 存在且可被 load_ab_gold 解析。"""
    assert AB_GOLD_PATH.exists(), f"缺少合成 gold fixture：{AB_GOLD_PATH}"
    cases = load_ab_gold(AB_GOLD_PATH)
    assert cases
    for c in cases:
        assert isinstance(c, AbGoldCase)
        assert c.query.strip()
        assert c.relevant_chunk_ids
        assert all(isinstance(cid, str) for cid in c.relevant_chunk_ids)


def test_ab_gold_fixture_jsonl_well_formed():
    """fixture 每行是含 query/relevant_chunk_ids 的合法 JSON。"""
    for line in AB_GOLD_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        assert "query" in record
        assert "relevant_chunk_ids" in record
        assert isinstance(record["relevant_chunk_ids"], list)


# ===========================================================================
# CLI：scripts/eval_retrieval_ab.py
# ===========================================================================

def test_cli_runs_and_prints_comparison_table(capsys):
    """CLI 对随附合成 gold 实跑：退出码 0，打印同时含 bm25 与 hybrid 的对照表。"""
    rc = ab_main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "recall" in out.lower()
    assert "bm25" in out.lower()
    assert "hybrid" in out.lower()


def test_cli_embedding_deterministic_explicit(capsys):
    """显式 --embedding deterministic 也能跑通并产出对照表。"""
    rc = ab_main(["--embedding", "deterministic"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hybrid" in out.lower()


# ===========================================================================
# --corpus 语料构建：从 jsonl 建块库（每篇单段 -> '<doc_id>#c0'），供真实标注 A/B 用
# ===========================================================================

def test_build_corpus_chunk_db_assigns_c0_ids(tmp_path):
    """user story —— 从语料 jsonl 建块库：每篇单段产出稳定 chunk_id '<doc_id>#c0'，
    gold 据此标注 relevant_chunk_ids。"""
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        '{"doc_id": "A.pptx", "text": "香港营收增长。"}\n'
        '{"doc_id": "B.pptx", "text": "group solvency stayed strong."}\n',
        encoding="utf-8",
    )
    db = tmp_path / "c.db"
    _build_corpus_chunk_db(db, corpus)

    store = ChunkStore(db)
    store.init_schema()
    try:
        index = NarrativeIndex(store, embedding_backend=None)
        got = {r.chunk.chunk_id for r in index.retrieve("香港营收", rerank=False, top_k=10)}
    finally:
        store.close()
    assert "A.pptx#c0" in got


def test_run_ab_over_built_corpus_computes_metrics(tmp_path):
    """user story —— 用 --corpus 建库 + gold 跑 run_ab：BM25 命中词法 query，指标落在 [0,1]。"""
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        '{"doc_id": "A.pptx", "text": "香港营收增长强劲。"}\n'
        '{"doc_id": "B.pptx", "text": "无关的板球比赛报道。"}\n',
        encoding="utf-8",
    )
    db = tmp_path / "c.db"
    _build_corpus_chunk_db(db, corpus)
    cases = [AbGoldCase(query="香港 营收 增长", relevant_chunk_ids=["A.pptx#c0"])]

    report = run_ab(db, cases, embedding_backend=None, k=5)
    assert 0.0 <= report["bm25"]["recall_at_k"] <= 1.0
    assert report["bm25"]["recall_at_k"] == 1.0  # 词法 query 必命中
