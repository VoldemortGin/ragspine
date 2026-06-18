"""storage —— sqlite 存储层：数值指标事实表（fact_metric），全程保留来源 lineage。

每条事实携带 source_doc_id + 定位，绝不丢失来源。

Submodules:
    fact_store.py — 指标事实表 fact_metric 的存储层（stdlib sqlite3）。
"""
