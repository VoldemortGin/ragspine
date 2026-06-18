"""管线拓扑导出 CLI：把 agent / retriever / service 的静态拓扑导成 Mermaid/DOT/JSON。

用法（从项目根目录）：
    python scripts/topology.py                                  # agent → Mermaid → stdout
    python scripts/topology.py --which retriever --of dot
    python scripts/topology.py --of json --out docs/generated/topology.json

输出建议写到【git-ignored】的 docs/generated/ 目录（重生成、不入 diff）。本脚本零依赖、
离线确定：从真实装配（agent_topology / HybridRetriever / create_app）派生图，不发任何网络调用。
"""

import argparse
import json
import os
import sys
from pathlib import Path

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.pipeline import PipelineGraph, agent_topology, retriever_topology, service_topology
from ragspine.retrieval.lexical.retrieval import HybridRetriever
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import FakeQueue


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RAGSpine 管线拓扑导出（Mermaid/DOT/JSON）",
        epilog="提示：--out 建议写到 git-ignored 的 docs/generated/（如 docs/generated/topology.mmd）。",
    )
    parser.add_argument(
        "--of", dest="fmt", choices=["mermaid", "dot", "json"], default="mermaid",
        help="导出格式：mermaid（默认，GitHub 内联渲染）| dot（Graphviz）| json（自渲染/喂给别的工具）",
    )
    parser.add_argument(
        "--which", choices=["agent", "retriever", "service"], default="agent",
        help="导出哪张拓扑：agent（默认，完整请求流）| retriever（检索子管线）| service（服务层）",
    )
    parser.add_argument(
        "--out", default=None,
        help="输出文件路径（默认写到 stdout）；建议 docs/generated/ 下",
    )
    return parser


def _build_graph(which: str) -> PipelineGraph:
    """按 --which 构建拓扑：用默认/离线装配反映完整接线（不发网络调用）。"""
    if which == "retriever":
        # 默认 HybridRetriever（空块库）：纯 BM25 骨架，反映「现状」装配。
        return retriever_topology(HybridRetriever([]))
    if which == "service":
        # 用 FakeQueue 避免依赖 Redis；FAQ 短路在 agent 上游，异步路径俱全。
        app = create_app(
            ServiceConfig.from_env(),
            provider=MockProvider(),
            queue=FakeQueue(),
            faq_cache=FAQCache.empty(),
        )
        return service_topology(app)
    # agent：注入 narrative_retriever 哨兵，使叙事/composite 分支也出现（完整请求流）。
    return agent_topology(narrative_retriever=object())


def _render(graph: PipelineGraph, fmt: str) -> str:
    if fmt == "mermaid":
        return graph.to_mermaid()
    if fmt == "dot":
        return graph.to_dot()
    return json.dumps(graph.to_dict(), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    graph = _build_graph(args.which)
    text = _render(graph, args.fmt)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"已写入 {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
