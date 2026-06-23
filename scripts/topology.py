"""Thin CLI shim -> ragspine.cli.topology（逻辑在 src）。用法见模块 docstring。"""

from ragspine.cli.topology import main

if __name__ == "__main__":
    raise SystemExit(main())
