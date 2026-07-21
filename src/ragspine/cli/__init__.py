"""ragspine 控制台 CLI：把零-SDK 离线核心包装成一条可直接运行的命令。

`pip install rag-spine` 后即得到 `ragspine` 命令（入口 `ragspine.cli:main`），
无需再走 `.venv/bin/python scripts/...`。每条子命令都是包内 API 的薄封装，
绝不 shell 调 scripts/（后者不进 wheel）。headline 子命令 `quickstart` 全程离线、
零 API key，秒级演示反幻觉（坦白拒答、绝不臆造）与来源溯源（每个数字带血缘）。

Submodules:
    main.py — argparse 子命令分发（quickstart / ask / version）；公开入口 main()。
    serve.py — 本地 workspace 的 loopback API + Studio 启动装配。
    ask.py — 单条提问 CLI 逻辑（意图解析 → tool-use 循环 → 确定值 + 血缘）。
    ingest.py — 结构化入库生产 CLI 逻辑（xlsx/pptx/pdf -> fact_store）。
    ingest_narrative.py — 叙事语料批量入库 CLI 逻辑（文件夹/文件 -> 块库）。
    topology.py — 管线拓扑导出 CLI 逻辑（agent/retriever/service -> Mermaid/DOT/JSON）。
    eval_retrieval_ab.py — BM25-only vs hybrid 检索 A/B harness 逻辑。
    run_qa_eval.py — Q&A 评测闭环 CLI 逻辑（四命门指标 + 基线门禁）。
"""

from ragspine.cli.main import main

__all__ = ["main"]
