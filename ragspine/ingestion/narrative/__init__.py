"""narrative —— 叙事通路入库：真实文档 → 文本+定位段 → 切块 → 块库。

抽取产物可直接喂给 retrieval 的 chunking；批量编排幂等、可 dry-run。

Submodules:
    narrative_extract.py — 叙事文本抽取：文档 → 文档文本 + 定位段。
    narrative_ingest.py — 叙事批量入库编排：文件夹/文件列表 → 抽取 → 切块 → 块库。
"""
