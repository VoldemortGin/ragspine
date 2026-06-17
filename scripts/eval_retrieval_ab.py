"""BM25-only vs hybrid 检索 A/B harness（GAP-A 向量通道，确定性、可复现）。

给定一份块库（chunk_db）与一份 gold（jsonl：每行 {query, relevant_chunk_ids:[...]}），
分别用 BM25-only（embedding_backend=None）与 hybrid（注入某 EmbeddingBackend）跑同一套
NarrativeIndex 检索，计算并对比 Recall@k / MRR，打印对照表。

用法（从项目根目录）：
    python scripts/eval_retrieval_ab.py
        # 默认对随附合成 gold（data/golden/retrieval_ab_sample.jsonl）跑，hybrid 用确定性后端
    python scripts/eval_retrieval_ab.py --chunk-db <path> --gold <jsonl> --k 10
    python scripts/eval_retrieval_ab.py --embedding deterministic

**诚实边界（不可省略）**：随附的合成 gold 仅用于证明 **harness 本身算得对、跑得通**
（指标公式、双臂构建、确定性复现），不代表任何真实检索质量；hybrid 默认用的
DeterministicEmbeddingBackend 是【词法散列、非语义】后端，与 BM25 高度相关，**不应、
也不被断言带来语义增益**。真实 recall 对比需真实样本标注 + 真模型（Qwen3 等，待 GPU infra）。
"""

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.retrieval.vector.embedding_backends import make_embedding_backend
from ragspine.retrieval.lexical.retrieval import EmbeddingBackend, NarrativeIndex

# 随附合成 gold fixture（git 版本化）：证明 harness 正确，非真实标注。
AB_GOLD_PATH = ROOT_DIR / "data" / "golden" / "retrieval_ab_sample.jsonl"

# 与合成 gold 同源的小型块库（按需就地建，避免外部状态依赖）。
_SAMPLE_DOCS = (
    ("HK_REVENUE.pptx", "香港 REVENUE 营收持续增长，银保渠道表现稳健。"),
    ("CN_REVENUE.pptx", "中国 REVENUE 增长由代理人产能提升与银保渠道扩张驱动。"),
    ("HK_REG.pptx", "香港监管动态：MPFA 强积金新规要求披露管理费。"),
    ("OTHER.pptx", "weekend cricket match report 板球比赛与本业务无关。"),
)


@dataclass
class AbGoldCase:
    """一条 A/B gold：查询 + 该查询的相关块 id 列表（人工/合成标注）。"""

    query: str
    relevant_chunk_ids: list[str]


def compute_recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Recall@k：retrieved 前 k 命中的 relevant 占全部 relevant 的比例。

    relevant 为空时约定返回 1.0（无可召回项即满分，不污染均值）。
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 1.0
    top_k = set(retrieved[:k])
    return len(relevant_set & top_k) / len(relevant_set)


def compute_mrr(retrieved: list[str], relevant: list[str]) -> float:
    """MRR：retrieved 中第一个命中 relevant 的名次倒数（rank 从 1 起）；无命中 0.0。"""
    relevant_set = set(relevant)
    for rank, item in enumerate(retrieved, start=1):
        if item in relevant_set:
            return 1.0 / rank
    return 0.0


def load_ab_gold(path: str | Path) -> list[AbGoldCase]:
    """读 jsonl gold（每行 {query, relevant_chunk_ids:[...]}），空行跳过。"""
    cases: list[AbGoldCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        cases.append(
            AbGoldCase(
                query=record["query"],
                relevant_chunk_ids=list(record["relevant_chunk_ids"]),
            )
        )
    return cases


def _retrieved_ids(index: NarrativeIndex, query: str, k: int) -> list[str]:
    """对单查询跑检索（关闭 listwise 二审，纯检索序），返回前 k 个 chunk_id。"""
    results = index.retrieve(query, rerank=False, top_k=k)
    return [r.chunk.chunk_id for r in results]


def _eval_arm(
    chunk_db: str | Path,
    cases: list[AbGoldCase],
    *,
    embedding_backend: EmbeddingBackend | None,
    k: int,
) -> dict[str, float]:
    """单臂评测：用给定后端建 NarrativeIndex，跑全部 gold，返回均值 Recall@k / MRR。"""
    store = ChunkStore(chunk_db)
    store.init_schema()
    try:
        index = NarrativeIndex(store, embedding_backend=embedding_backend)
        recalls: list[float] = []
        mrrs: list[float] = []
        for case in cases:
            retrieved = _retrieved_ids(index, case.query, k)
            recalls.append(compute_recall_at_k(retrieved, case.relevant_chunk_ids, k))
            mrrs.append(compute_mrr(retrieved, case.relevant_chunk_ids))
        n = len(cases)
        return {
            "recall_at_k": sum(recalls) / n if n else 0.0,
            "mrr": sum(mrrs) / n if n else 0.0,
        }
    finally:
        store.close()


def run_ab(
    chunk_db: str | Path,
    cases: list[AbGoldCase],
    *,
    embedding_backend: EmbeddingBackend | None = None,
    k: int = 10,
) -> dict[str, dict[str, float]]:
    """对同一块库与 gold 跑 BM25-only 与 hybrid 两臂，返回 {arm: {recall_at_k, mrr}}。

    - bm25 臂恒用 embedding_backend=None（纯 BM25+RRF）；
    - hybrid 臂用传入的 embedding_backend；为 None 时退化为与 bm25 臂同结果
      （此时两臂相等，仅证明 harness 双臂路径都跑得通）。
    确定性、可复现：相同输入两次调用结果逐键相等。
    """
    return {
        "bm25": _eval_arm(chunk_db, cases, embedding_backend=None, k=k),
        "hybrid": _eval_arm(chunk_db, cases, embedding_backend=embedding_backend, k=k),
    }


def _build_sample_chunk_db(path: Path) -> None:
    """就地建与合成 gold 同源的小型块库（每篇单段 -> chunk_id '<doc>#c0'）。"""
    from ragspine.retrieval.chunking.chunking import DocumentMeta

    store = ChunkStore(path)
    store.init_schema()
    try:
        index = NarrativeIndex(store)
        for doc_id, text in _SAMPLE_DOCS:
            index.ingest(
                text,
                DocumentMeta(
                    doc_id=doc_id, title=doc_id, topic="FIN", entity="ACME_HK",
                    geography="HK", period="2025", language="zh", sensitivity="INTERNAL",
                ),
            )
    finally:
        store.close()


def _build_corpus_chunk_db(path: Path, corpus_jsonl: str | Path) -> None:
    """从语料 jsonl 建块库：每行 {doc_id, text, ...可选 meta}，每篇单段 -> '<doc_id>#c0'。

    text 应保持在切块预算内（单段），使 chunk_id 稳定为 '<doc_id>#c0'，gold 据此标注。
    """
    from ragspine.retrieval.chunking.chunking import DocumentMeta

    store = ChunkStore(path)
    store.init_schema()
    try:
        index = NarrativeIndex(store)
        for line in Path(corpus_jsonl).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            doc_id = rec["doc_id"]
            index.ingest(
                rec["text"],
                DocumentMeta(
                    doc_id=doc_id,
                    title=rec.get("title", doc_id),
                    topic=rec.get("topic", "FIN"),
                    entity=rec.get("entity", "ACME_GROUP"),
                    geography=rec.get("geography", "ASIA"),
                    period=rec.get("period", "2025"),
                    language=rec.get("language", "zh"),
                    sensitivity=rec.get("sensitivity", "INTERNAL"),
                ),
            )
    finally:
        store.close()


def _format_table(
    report: dict[str, dict[str, float]], k: int, *, label: str = "合成 gold", note: str | None = None
) -> str:
    """对照表（纯文本，确定性）：两臂 Recall@k / MRR 并列。

    label/note 缺省时输出与历史一致（合成 gold + 非语义 caveat）；真实跑（qwen3+真实标注）
    时由调用方覆盖，避免误导性 caveat。
    """
    if note is None:
        note = (
            "注：合成 gold 仅证明 harness 正确；hybrid 用确定性词法散列后端（非语义），"
            "不主张语义增益。真实 recall 需真实样本+真模型（待 GPU infra）。"
        )
    lines = [
        f"BM25-only vs hybrid 检索对照（{label}，k={k}）",
        f"{'arm':<10}{'Recall@k':>12}{'MRR':>12}",
        "-" * 34,
    ]
    for arm in ("bm25", "hybrid"):
        m = report[arm]
        lines.append(f"{arm:<10}{m['recall_at_k']:>12.4f}{m['mrr']:>12.4f}")
    lines.append(note)
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BM25-only vs hybrid 检索 A/B harness")
    parser.add_argument(
        "--chunk-db", default=None,
        help="块库 sqlite 路径；不传则就地建与合成 gold 同源的小型块库",
    )
    parser.add_argument(
        "--gold", default=str(AB_GOLD_PATH),
        help=f"gold jsonl 路径（默认随附合成 gold {AB_GOLD_PATH}）",
    )
    parser.add_argument(
        "--corpus", default=None,
        help="语料 jsonl（每行 {doc_id, text, ...可选 meta}）；传入则由它建块库，"
             "每篇单段 -> chunk_id '<doc_id>#c0'，gold 的 relevant_chunk_ids 据此写",
    )
    parser.add_argument(
        "--embedding",
        choices=["none", "deterministic", "openai", "qwen3", "sentence-transformers", "st"],
        default="deterministic",
        help="hybrid 臂的向量后端（deterministic＝离线词法散列非语义；qwen3＝真实语义后端，"
             "设备自适应 cuda/mps/cpu，需 [embed] extra）",
    )
    parser.add_argument("--k", type=int, default=10, help="Recall@k 的 k（默认 10）")
    return parser


def main(argv: list[str] | None = None) -> int:
    import tempfile

    args = _build_parser().parse_args(argv)
    cases = load_ab_gold(args.gold)
    embedding_backend = make_embedding_backend(args.embedding)

    real_run = args.embedding in ("qwen3", "sentence-transformers", "st")
    if real_run:
        label = f"真实语义后端 {args.embedding}"
        note = (
            f"注：hybrid 用真实语义后端（{args.embedding}，设备自适应 cuda/mps/cpu）；"
            "语料/标注见 --corpus/--gold。hybrid 高于 bm25 即为向量通道在该集上的语义召回增益。"
        )
    else:
        label, note = "合成 gold", None

    if args.chunk_db:
        report = run_ab(args.chunk_db, cases, embedding_backend=embedding_backend, k=args.k)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ab_chunks.db"
            if args.corpus:
                _build_corpus_chunk_db(db, args.corpus)
            else:
                _build_sample_chunk_db(db)
            report = run_ab(db, cases, embedding_backend=embedding_backend, k=args.k)

    print(_format_table(report, args.k, label=label, note=note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
