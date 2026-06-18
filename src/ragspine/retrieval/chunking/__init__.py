"""chunking —— 叙事切块：文档级纯文本+元数据 → Chunk 列表，并落入版本化块库。

块库模式仿 fact_store：显式 schema、参数化 SQL、只读 execute_read 入口。

Submodules:
    chunk_store.py — 叙事块库（sqlite，显式 schema、参数化 SQL、execute_read 只读入口）。
    chunking.py — 叙事通路切块器：文档级纯文本 + 元数据 → Chunk 列表。
"""
