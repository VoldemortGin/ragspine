"""Thin CLI shim -> ragspine.cli.ask（逻辑在 src）。用法见模块 docstring。"""

from ragspine.cli.ask import main

if __name__ == "__main__":
    raise SystemExit(main())
