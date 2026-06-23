"""Thin CLI shim -> ragspine.cli.ingest_narrative（逻辑在 src）。用法见模块 docstring。"""

from ragspine.cli.ingest_narrative import main

if __name__ == "__main__":
    raise SystemExit(main())
