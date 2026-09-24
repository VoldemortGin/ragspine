"""页级父子开关关闭（默认 off）时，叙事检索输出与引入开关之前逐字节一致。

冻结摘要取自引入 page_parent 之前的实现（HEAD 4e498b3）：同一语料、同一组 query，
纯 BM25 与「确定性向量 + 倒序 judge 精排」两种装配下 snippet 的 JSON 摘要。
默认构造与显式 page_parent="off" 必须命中同一摘要。
"""

import hashlib
import json
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend

from .conftest import page_corpus

_QUERIES = ("Singapore VONB", "Hong Kong revenue", "Singapore growth outlook", "margins")

_FROZEN = "032911978fc78f055c68545d3737f3404be3eaea54b31cf5e90ac56af66a9f79"


class _ReverseJudge:
    def judge(self, query: str, candidates: list[str]) -> list[int]:
        return list(reversed(range(len(candidates))))


def _digest(page_store, **index_kwargs) -> str:
    dumps = []
    for hybrid in (False, True):
        index = NarrativeIndex(
            page_store,
            embedding_backend=DeterministicEmbeddingBackend() if hybrid else None,
            judge=_ReverseJudge() if hybrid else None,
            **index_kwargs,
        )
        if hybrid:
            # 向量在入库时写进 store：按块重放一次 ingest 的嵌入写入（与 NarrativeIndex.ingest 同口径）。
            from ragspine.retrieval.lexical.retrieval import _record_metadata
            from ragspine.retrieval.vector.store import VectorRecord

            chunks = [c for cs in page_corpus().values() for c in cs]
            vectors = index.embedding_backend.embed_texts([c.text for c in chunks])
            index.vector_store.upsert(
                [
                    VectorRecord(id=c.chunk_id, vector=tuple(v), metadata=_record_metadata(c))
                    for c, v in zip(chunks, vectors, strict=True)
                ]
            )
        retriever = NarrativeIndexRetriever(index)
        for query in _QUERIES:
            for top_k in (2, 50):
                dumps.append(retriever.retrieve(query, top_k=top_k))
    payload = json.dumps(dumps, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_default_matches_frozen_snapshot(page_store):
    assert _digest(page_store) == _FROZEN


def test_explicit_off_matches_frozen_snapshot(page_store):
    assert _digest(page_store, page_parent="off") == _FROZEN
