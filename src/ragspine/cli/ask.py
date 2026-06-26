"""单条提问 CLI：意图解析 → 澄清网关 → tool use 循环 → 确定值 + 血缘。

用法（从项目根目录）：
    python scripts/ask.py --provider mock "香港去年REVENUE多少"
    python scripts/ask.py --provider anthropic --base-url https://gw.example.com "..."
mock 模式离线确定性，不需要任何 API key。
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from ragspine.agent.agent import answer_question
from ragspine.agent.llm_provider import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
    LLMProvider,
    MockProvider,
)
from ragspine.common.core import DEFAULT_FACT_DB
from ragspine.retrieval.link.narrative_link import build_narrative_retriever
from ragspine.retrieval.vector.embedding_backends import make_embedding_backend
from ragspine.storage.fact_store import FactStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAGSpine 单条提问")
    parser.add_argument("question", help="用户问题，如：香港去年REVENUE多少")
    parser.add_argument(
        "--provider", choices=["mock", "anthropic"], default="mock",
        help="mock=离线确定性（默认）；anthropic=真实 Claude 调用",
    )
    parser.add_argument("--db", default=str(DEFAULT_FACT_DB), help="fact_metric sqlite 路径")
    parser.add_argument(
        "--chunk-db", default=None,
        help="叙事块库 sqlite 路径；提供时 narrative/composite 问题走真实检索"
             "（纯 BM25+RRF + listwise 二审），不提供时保持坦白降级",
    )
    parser.add_argument(
        "--embedding", choices=["none", "auto", "onnx", "deterministic", "openai"], default="none",
        help="叙事检索向量通道后端：none=纯 BM25（默认，现状不变）；"
             "auto=装了 [embed-onnx] 走真语义 ONNX、否则回落纯 BM25；"
             "onnx=真语义 ONNX 句向量（fastembed，需 [embed-onnx]）；"
             "deterministic=离线词法散列后端（非语义，打通管线用）；"
             "openai=OpenAI embeddings（需装 SDK 与配置）",
    )
    parser.add_argument(
        "--reference-date", default=None,
        help="相对期间换算基准日 YYYY-MM-DD（默认今天）",
    )
    parser.add_argument(
        "--model", default=DEFAULT_ANTHROPIC_MODEL,
        help=f"anthropic 模型名（默认 {DEFAULT_ANTHROPIC_MODEL}）",
    )
    parser.add_argument(
        "--base-url", default=None,
        help="覆盖 Anthropic API base_url（适配企业网关）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # ADR 0012 rule 5：fact 库缺失且无叙事库可答时大声报错，绝不静默建空库再返回假「查不到」。
    # 提供 --chunk-db 即「纯叙事」合法用法——此时空 fact 库无妨（答案来自叙事块库），放行。
    if str(args.db) != ":memory:" and not Path(args.db).exists() and not args.chunk_db:
        print(
            f"error: fact 库不存在：{args.db}\n"
            "请用 --db 指定有效的 fact_metric sqlite 路径，"
            "或先跑 `python scripts/run_demo.py` 生成示例库，"
            "或用 `ragspine quickstart` 体验离线演示。",
            file=sys.stderr,
        )
        return 2

    reference_date = (
        date.fromisoformat(args.reference_date) if args.reference_date else None
    )

    provider: LLMProvider
    if args.provider == "anthropic":
        provider = AnthropicProvider(model=args.model, base_url=args.base_url)
    else:
        provider = MockProvider(reference_date=reference_date)

    narrative_retriever = None
    chunk_store = None
    if args.chunk_db:
        embedding_backend = make_embedding_backend(args.embedding)
        narrative_retriever, chunk_store = build_narrative_retriever(
            args.chunk_db, provider=provider, embedding_backend=embedding_backend
        )

    store = FactStore(args.db)
    store.init_schema()
    try:
        result = answer_question(
            args.question, store, provider, reference_date=reference_date,
            narrative_retriever=narrative_retriever,
        )
    finally:
        store.close()
        if chunk_store is not None:
            chunk_store.close()

    print(result.answer)
    if result.sources:
        print("\n数据血缘：")
        for src in result.sources:
            print(f"  - {src['doc']} · {src['locator']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
