"""chunking —— 叙事切块：文档级纯文本+元数据 → Chunk 列表，并落入版本化块库。

块库模式仿 fact_store：显式 schema、参数化 SQL、只读 execute_read 入口。

Submodules:
    chunk_store.py — 叙事块库（sqlite，显式 schema、参数化 SQL、execute_read 只读入口）。
    chunker.py — Chunker 缝：可插拔切块策略 Protocol + 行为等价的默认实现（薄壳委托）。
    chunking.py — 叙事通路切块器：文档级纯文本 + 元数据 → Chunk 列表。
    layout_chunker.py — 布局感知 + 父子切块策略（W4b，标题边界切 + parent_id/heading）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
