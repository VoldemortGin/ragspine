"""查询翻译开关为 off（RAGSPINE_QUERY_TRANSLATION=off）时，叙事检索输出逐字节不变。

冻结摘要取自引入开关之前的实现（HEAD 43d3b73）：英文带标题语料、中英文 query 混合，三种页级父子模式 ×
（纯 BM25 / 确定性向量 + 倒序 judge 精排）下 snippet 的 JSON 摘要。默认构造、显式 query_translator=None、
build_narrative_retriever(query_translation="off", 带会翻译的 provider) 必须命中同一摘要。
"""

import hashlib
import json
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.lexical.retrieval import NarrativeIndex, _record_metadata
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend
from ragspine.retrieval.vector.store import VectorRecord

from .conftest import TranslatingProvider, heading_corpus

_QUERIES = ("分销渠道 占比", "新加坡 VONB", "distribution mix", "Singapore VONB")
_TABLE = {"分销渠道 占比": "Distribution Mix share", "新加坡 VONB": "Singapore VONB"}

_FROZEN_RETRIEVAL = "39f81bfac5af0f76f81e397dbb09cf5c10bb95f2ad43902166fea7d890743451"


class _ReverseJudge:
    def judge(self, query: str, candidates: list[str]) -> list[int]:
        return list(reversed(range(len(candidates))))


def _digest(store, make_index) -> str:
    dumps = []
    for page_parent in ("off", "dedup", "page+child"):
        for hybrid in (False, True):
            index = make_index(
                store,
                embedding_backend=DeterministicEmbeddingBackend() if hybrid else None,
                judge=_ReverseJudge() if hybrid else None,
                page_parent=page_parent,
            )
            if hybrid:
                chunks = [c for cs in heading_corpus().values() for c in cs]
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


def test_default_matches_frozen_snapshot(en_store):
    assert _digest(en_store, NarrativeIndex) == _FROZEN_RETRIEVAL


def test_explicit_no_translator_matches_frozen_snapshot(en_store):
    def make(store, **kw):
        return NarrativeIndex(store, query_translator=None, **kw)

    assert _digest(en_store, make) == _FROZEN_RETRIEVAL


def test_build_narrative_retriever_off_matches_frozen_snapshot(en_store, tmp_path):
    from ragspine.retrieval.link.narrative_link import build_narrative_retriever

    db = tmp_path / "chunks.db"
    opened_stores = []
    provider = TranslatingProvider(_TABLE)

    def make(store, *, embedding_backend, judge, page_parent):
        retriever, opened = build_narrative_retriever(
            db,
            embedding_backend=embedding_backend,
            reranker=judge,
            page_parent=page_parent,
            query_translation="off",
            translation_provider=provider,
        )
        opened_stores.append(opened)
        return retriever.index

    try:
        assert _digest(en_store, make) == _FROZEN_RETRIEVAL
    finally:
        for opened in opened_stores:
            opened.close()
    assert provider.calls == []
