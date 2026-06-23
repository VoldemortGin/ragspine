"""Thin CLI shim -> ragspine.cli.run_qa_eval（逻辑在 src）。用法见模块 docstring。"""

from ragspine.cli.run_qa_eval import main

if __name__ == "__main__":
    raise SystemExit(main())
