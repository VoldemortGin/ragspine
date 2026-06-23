"""Thin CLI shim -> ragspine.cli.eval_retrieval_ab（逻辑在 src）。用法见模块 docstring。"""

from ragspine.cli.eval_retrieval_ab import main

if __name__ == "__main__":
    raise SystemExit(main())
