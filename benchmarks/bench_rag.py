#!/usr/bin/env python3
"""RAGSpine 离线性能 benchmark —— 零真实 API、确定性、纯标准库（timeit / perf_counter）。

覆盖 RAG 关键路径（全部用 MockProvider / 离线确定性 embedder / 内存 store，绝不发任何网络请求）：

  1. embed   —— DeterministicEmbeddingBackend.embed_texts：一批文本的离线编码吞吐
  2. vector  —— InProcessVectorStore.upsert + .query(top-k)：内存向量库 add + 检索延迟
  3. chunk   —— chunk_document：长文切块吞吐
  4. retrieve—— NarrativeIndex.ingest + .retrieve：建库 + 混合检索（纯 BM25 / 确定性向量两档）
  5. answer  —— answer_question + MockProvider + FactStore(:memory:) + NarrativeIndexRetriever：
               端到端单条问答延迟（结构化 + 叙事 composite 路）

从【包根】直接跑（不进子目录）：

    .venv/bin/python benchmarks/bench_rag.py            # 全部
    .venv/bin/python benchmarks/bench_rag.py embed vector  # 只跑指定项
    .venv/bin/python benchmarks/bench_rag.py --json      # 机读输出（基线归档用）

每项报告 median / mean（多次重复取中位数为基线，抗抖动）。所有语料为合成确定性数据，
非真实公司数据；与 data/golden 无关，绝不写盘。
"""

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable
from datetime import date

import rootutils

ROOT_DIR = rootutils.setup_root(__file__, indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question
from ragspine.agent.llm_provider import MockProvider
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.chunking.chunking import DocumentMeta, chunk_document
from ragspine.retrieval.lexical.retrieval import NarrativeIndex
from ragspine.retrieval.link.narrative_link import NarrativeIndexRetriever
from ragspine.retrieval.vector.embedding_backends import DeterministicEmbeddingBackend
from ragspine.retrieval.vector.store import InProcessVectorStore, VectorRecord
from ragspine.storage.fact_store import Fact, SqliteFactStore

REF = date(2026, 6, 12)

# --- 合成确定性语料（无真实数据；中英混排，贴近 ragspine 的 CJK 分词口径）-----------------

_SENTENCES = [
    "香港 REVENUE 在 FY2025 同比下降，主因是 MCV 客群收缩与银保渠道结构调整。",
    "中国内地 NBV 受新单结构与产品组合影响，利润率口径较去年同期有所改善。",
    "ACME 的 VONB margin 在亚太区维持稳健，代理人渠道贡献占比持续提升。",
    "The agency channel delivered resilient new business value across the region.",
    "Bancassurance partnerships contributed steady protection-oriented product mix.",
    "总精算师指出，准备金假设更新对当期 IFRS17 合同服务边际释放节奏有影响。",
    "Embedded value sensitivity to interest rate movements remained within tolerance.",
    "新加坡市场的高净值客群需求带动储蓄型产品销售环比增长。",
]


def _doc_text(n_sentences: int) -> str:
    """拼出确定性长文（句子按序循环），用于切块 / 建库 / 检索。"""
    return "".join(_SENTENCES[i % len(_SENTENCES)] for i in range(n_sentences))


def _corpus(n_docs: int, sentences_per_doc: int) -> list[tuple[str, DocumentMeta]]:
    """合成 n_docs 篇带元数据的文档（确定性）。"""
    docs: list[tuple[str, DocumentMeta]] = []
    for i in range(n_docs):
        meta = DocumentMeta(
            doc_id=f"SYNTH_DOC_{i:04d}.pptx",
            title=f"合成季度回顾 {i}",
            entity="ACME_HK" if i % 2 == 0 else "ACME_CN",
            geography="HK" if i % 2 == 0 else "CN",
            period="2025",
        )
        docs.append((_doc_text(sentences_per_doc), meta))
    return docs


# --- 计时原语（纯标准库 perf_counter；repeat 取中位数为基线）----------------------------


def _time_once(fn: Callable[[], object]) -> float:
    """跑一次 fn，返回耗时（秒，perf_counter 高精度）。"""
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def _measure(fn: Callable[[], object], *, repeat: int, warmup: int = 1) -> dict[str, float]:
    """重复计时取统计：返回 median / mean / min（秒）。warmup 次不计入。"""
    for _ in range(warmup):
        fn()
    samples = [_time_once(fn) for _ in range(repeat)]
    return {
        "median_s": statistics.median(samples),
        "mean_s": statistics.fmean(samples),
        "min_s": min(samples),
        "repeat": repeat,
    }


# --- 各项 benchmark：返回 (label, metrics, extra) -----------------------------------------


def bench_embed() -> dict[str, object]:
    """离线确定性 embedder 对一批文本编码的吞吐。"""
    backend = DeterministicEmbeddingBackend()  # dim=256
    batch = [_doc_text(4) for _ in range(512)]  # 512 条文本/批
    stats = _measure(lambda: backend.embed_texts(batch), repeat=20)
    n = len(batch)
    return {
        "name": "embed",
        "desc": "DeterministicEmbeddingBackend.embed_texts (512 texts/batch, dim=256)",
        "stats": stats,
        "throughput_texts_per_s": n / stats["median_s"],
        "n_texts": n,
    }


def bench_vector() -> dict[str, object]:
    """内存向量库 upsert（建库）+ query（top-k 检索）两段分别计时。"""
    backend = DeterministicEmbeddingBackend()
    n_records = 5000
    texts = [_doc_text(4) for _ in range(n_records)]
    vectors = backend.embed_texts(texts)
    records = [
        VectorRecord(id=f"chunk_{i}", vector=tuple(vectors[i]), metadata={"doc_id": f"d{i % 100}"})
        for i in range(n_records)
    ]
    query_vec = vectors[0]

    def _upsert() -> None:
        store = InProcessVectorStore()
        store.upsert(records)

    upsert_stats = _measure(_upsert, repeat=20)

    store = InProcessVectorStore()
    store.upsert(records)
    query_stats = _measure(lambda: store.query(query_vec, k=10), repeat=50)

    return {
        "name": "vector",
        "desc": f"InProcessVectorStore upsert({n_records}) / query(top-k=10) over {n_records} brute-force",
        "n_records": n_records,
        "upsert_stats": upsert_stats,
        "query_stats": query_stats,
        "query_throughput_per_s": 1.0 / query_stats["median_s"],
    }


def bench_chunk() -> dict[str, object]:
    """长文切块吞吐（chunk_document）。"""
    text = _doc_text(2000)  # 约 2000 句的长文
    meta = DocumentMeta(doc_id="SYNTH_LONG.pptx", entity="ACME_HK", period="2025")
    n_chars = len(text)
    stats = _measure(lambda: chunk_document(text, meta), repeat=30)
    return {
        "name": "chunk",
        "desc": f"chunk_document over {n_chars} chars (max_chars=480, overlap=80)",
        "stats": stats,
        "n_chars": n_chars,
        "throughput_chars_per_s": n_chars / stats["median_s"],
        "n_chunks": len(chunk_document(text, meta)),
    }


def _build_index(*, with_vector: bool) -> tuple[NarrativeIndex, ChunkStore, int]:
    """建一个内存叙事库并灌入合成语料；返回 (index, store, 入库块数)。judge=None 离线无二审。"""
    store = ChunkStore(":memory:")
    store.init_schema()
    backend = DeterministicEmbeddingBackend() if with_vector else None
    index = NarrativeIndex(store, embedding_backend=backend)
    n_chunks = 0
    for text, meta in _corpus(n_docs=200, sentences_per_doc=12):
        n_chunks += index.ingest(text, meta)
    return index, store, n_chunks


def bench_retrieve() -> dict[str, object]:
    """建库（ingest）+ 混合检索（retrieve）—— 纯 BM25 与确定性向量两档。"""
    results: dict[str, object] = {"name": "retrieve"}
    query = "香港 REVENUE 为什么下降"

    for label, with_vector in (("bm25", False), ("hybrid_deterministic_vector", True)):
        # ingest 段：每次重建一个干净内存库计时
        def _ingest() -> None:
            store = ChunkStore(":memory:")
            store.init_schema()
            backend = DeterministicEmbeddingBackend() if with_vector else None
            idx = NarrativeIndex(store, embedding_backend=backend)
            for text, meta in _corpus(n_docs=200, sentences_per_doc=12):
                idx.ingest(text, meta)
            store.close()

        ingest_stats = _measure(_ingest, repeat=5)

        # retrieve 段：库建一次，反复检索（rerank=False，judge=None 离线无二审）
        index, store, n_chunks = _build_index(with_vector=with_vector)
        retrieve_stats = _measure(
            lambda idx=index: idx.retrieve(query, top_k=10, rerank=False), repeat=30
        )
        store.close()

        results[label] = {
            "n_chunks": n_chunks,
            "ingest_stats": ingest_stats,
            "retrieve_stats": retrieve_stats,
            "retrieve_throughput_per_s": 1.0 / retrieve_stats["median_s"],
        }
    results["desc"] = "NarrativeIndex.ingest(200 docs) + .retrieve(top-k=10) — BM25 vs deterministic-vector hybrid"
    return results


def bench_answer() -> dict[str, object]:
    """端到端单条问答延迟：answer_question + MockProvider + FactStore + 叙事检索（composite 路）。"""
    fs = SqliteFactStore(":memory:")
    fs.init_schema()
    fs.upsert_facts(
        [
            Fact(
                metric_code="REVENUE",
                entity="ACME_HK",
                geography="HK",
                channel="TOTAL",
                period_type="FY",
                period="2025",
                value=1702.0,
                unit="USD_M",
                source_doc_id="ACME_FY2025_Results.pptx",
                source_locator="slide=5,table=1,row=2,col=3",
            )
        ]
    )

    chunk_store = ChunkStore(":memory:")
    chunk_store.init_schema()
    index = NarrativeIndex(chunk_store)  # judge=None：离线无二审
    for text, meta in _corpus(n_docs=50, sentences_per_doc=8):
        index.ingest(text, meta)
    retriever = NarrativeIndexRetriever(index)
    provider = MockProvider(reference_date=REF)

    question = "香港去年REVENUE多少，为什么下降了"

    def _ask() -> None:
        answer_question(
            question,
            fs,
            provider,
            reference_date=REF,
            narrative_retriever=retriever,
        )

    stats = _measure(_ask, repeat=30)
    # 取一次结果记录路由，确认确实走了 RAG 路（非 out-of-scope 拒答）
    sample = answer_question(
        question, fs, provider, reference_date=REF, narrative_retriever=retriever
    )
    chunk_store.close()
    fs.close()
    return {
        "name": "answer",
        "desc": "answer_question end-to-end (MockProvider + 50-doc narrative index + structured fact)",
        "stats": stats,
        "throughput_qps": 1.0 / stats["median_s"],
        "route": sample.route,
    }


BENCHES: dict[str, Callable[[], dict[str, object]]] = {
    "embed": bench_embed,
    "vector": bench_vector,
    "chunk": bench_chunk,
    "retrieve": bench_retrieve,
    "answer": bench_answer,
}


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.3f} ms"


def _print_human(result: dict[str, object]) -> None:
    name = result["name"]
    print(f"\n=== {name} ===")
    print(f"  {result['desc']}")
    if name == "embed":
        s = result["stats"]
        print(f"  median={_ms(s['median_s'])}  mean={_ms(s['mean_s'])}  (repeat={s['repeat']})")
        print(f"  throughput: {result['throughput_texts_per_s']:.0f} texts/s")
    elif name == "vector":
        u, q = result["upsert_stats"], result["query_stats"]
        print(f"  upsert({result['n_records']}): median={_ms(u['median_s'])}  mean={_ms(u['mean_s'])}")
        print(f"  query(top-k=10):  median={_ms(q['median_s'])}  mean={_ms(q['mean_s'])}")
        print(f"  query throughput: {result['query_throughput_per_s']:.0f} queries/s")
    elif name == "chunk":
        s = result["stats"]
        print(f"  median={_ms(s['median_s'])}  mean={_ms(s['mean_s'])}  ({result['n_chunks']} chunks)")
        print(f"  throughput: {result['throughput_chars_per_s'] / 1e6:.2f} M chars/s")
    elif name == "retrieve":
        for label in ("bm25", "hybrid_deterministic_vector"):
            r = result[label]
            i, rt = r["ingest_stats"], r["retrieve_stats"]
            print(f"  [{label}] {r['n_chunks']} chunks")
            print(f"     ingest(200 docs): median={_ms(i['median_s'])}  mean={_ms(i['mean_s'])}")
            print(f"     retrieve(top-k=10): median={_ms(rt['median_s'])}  mean={_ms(rt['mean_s'])}"
                  f"  ({r['retrieve_throughput_per_s']:.0f} q/s)")
    elif name == "answer":
        s = result["stats"]
        print(f"  route={result['route']}")
        print(f"  median={_ms(s['median_s'])}  mean={_ms(s['mean_s'])}  ({result['throughput_qps']:.0f} qps)")


def main() -> int:
    parser = argparse.ArgumentParser(description="RAGSpine 离线性能 benchmark（零 API）")
    parser.add_argument(
        "names",
        nargs="*",
        choices=list(BENCHES) + [],
        help="只跑指定项；缺省跑全部",
    )
    parser.add_argument("--json", action="store_true", help="机读 JSON 输出（基线归档用）")
    args = parser.parse_args()

    selected = args.names or list(BENCHES)
    results = [BENCHES[name]() for name in selected]

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        print(f"RAGSpine offline benchmark — python {sys.version.split()[0]}")
        for result in results:
            _print_human(result)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
