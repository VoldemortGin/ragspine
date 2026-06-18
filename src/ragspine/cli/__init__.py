"""ragspine 控制台 CLI：把零-SDK 离线核心包装成一条可直接运行的命令。

`pip install rag-spine` 后即得到 `ragspine` 命令（入口 `ragspine.cli:main`），
无需再走 `.venv/bin/python scripts/...`。每条子命令都是包内 API 的薄封装，
绝不 shell 调 scripts/（后者不进 wheel）。headline 子命令 `quickstart` 全程离线、
零 API key，秒级演示反幻觉（坦白拒答、绝不臆造）与来源溯源（每个数字带血缘）。

Submodules:
    main.py — argparse 子命令分发（quickstart / ask / version）；公开入口 main()。
"""

from ragspine.cli.main import main

__all__ = ["main"]
