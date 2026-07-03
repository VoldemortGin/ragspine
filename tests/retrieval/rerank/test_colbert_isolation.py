"""W11 隔离 conformance：ColBERT 晚交互重排路径下 RESTRICTED 候选绝不被打分/泄漏。

拍板（docs/invariants.md「RESTRICTED isolation 两出口」）：rerank 出口（listwise_rerank）把
sensitivity==RESTRICTED 的候选排除在 judge 之外（不送进打分、原位保留）。ColBERT 作为 ListwiseJudge
接入即【继承】此保护——本文件断言新 reranker 路径下 RESTRICTED 文本绝不进入 LateInteractionTextEmbedding.embed
的打分文档，并给出 reverse-proof（绕过编排缝直喂则会打分 -> 证明断言有牙）。范式同 W2 cross-encoder 隔离测试。
"""

import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.lexical.retrieval import RetrievalResult
from ragspine.retrieval.rerank.colbert import ColbertReranker
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY, listwise_rerank

SECRET = "SECRET-EXEC-PR 高管评级（RESTRICTED 不出域）"


def _result(i: int, text: str, sensitivity: str = "INTERNAL") -> RetrievalResult:
    chunk = Chunk(
        chunk_id=f"d{i}#c0",
        doc_id=f"d{i}",
        seq=0,
        text=text,
        source_locator=f"d{i}#para1",
        para_start=1,
        para_end=1,
        sensitivity=sensitivity,
    )
    return RetrievalResult(chunk=chunk, bm25_score=1.0, vector_score=0.0, fused_score=1.0 / (i + 1))


def _scored_docs(captured) -> list[str]:
    """所有进入 LateInteractionTextEmbedding.embed 的打分文档（跨多次调用展平）。"""
    return [doc for call in captured["embed_calls"] for doc in call]


def test_colbert_never_scores_restricted_via_listwise(fake_colbert):
    """RESTRICTED 候选经 listwise_rerank 喂给 ColBERT 时：其文本绝不进入 embed 打分文档。"""
    captured = fake_colbert()
    results = [
        _result(0, "a b public"),
        _result(1, SECRET, sensitivity=RESTRICTED_SENSITIVITY),
        _result(2, "a public"),
    ]
    out = listwise_rerank("a b", results, ColbertReranker())

    scored = _scored_docs(captured)
    assert SECRET not in scored
    assert all(SECRET not in doc for doc in scored)
    # ColBERT 只看到两条非 RESTRICTED 候选。
    assert scored == ["a b public", "a public"]
    # RESTRICTED 块原位保留（未被重排进 judge 输出）。
    assert out[1].chunk.text == SECRET


def test_all_restricted_colbert_not_called(fake_colbert):
    """候选全 RESTRICTED -> ColBERT 完全不被调用，整体退化为 RRF 序。"""
    captured = fake_colbert()
    results = [_result(i, f"secret {i}", sensitivity=RESTRICTED_SENSITIVITY) for i in range(3)]
    out = listwise_rerank("q", results, ColbertReranker())
    assert captured["embed_calls"] == []
    assert captured["query_calls"] == []
    assert [r.chunk.text for r in out] == ["secret 0", "secret 1", "secret 2"]


def test_restricted_case_insensitive_via_colbert(fake_colbert):
    """sensitivity 大小写不敏感：'restricted' 同样不进 ColBERT 打分。"""
    captured = fake_colbert()
    results = [_result(0, SECRET, sensitivity="restricted")]
    listwise_rerank("q", results, ColbertReranker())
    assert captured["embed_calls"] == []


def test_reverse_proof_direct_judge_would_score_restricted(fake_colbert):
    """reverse-proof：上面的断言不是空断言。

    若 RESTRICTED 文本【绕过】listwise_rerank 两出口缝、被直接喂给 reranker.judge，ColBERT
    【确实】会把它 embed 打分——这证明隔离来自编排缝（listwise_rerank），ColBERT 接入即继承；
    上面的 conformance 断言能抓住任何绕过该缝的回归（若 listwise_rerank 漏过 RESTRICTED，scored
    里就会出现 SECRET，断言即红）。
    """
    captured = fake_colbert()
    ColbertReranker().judge("a b", ["a b public", SECRET, "a public"])
    assert SECRET in _scored_docs(captured)
