"""structured —— 结构化事实入库：单文件 Excel 编排 + 幂等批量 manifest 台账。

入库链路 styled_grid 抽取 → glossary 归一 → 颜色 tags → 写入 fact_store；
manifest 台账兼作运维/管理面与幂等守护。

Submodules:
    ingestion_manifest.py — Ingestion manifest 台账 + 可观测指标 + 生产配置版本清单。
    ingestion.py — 单文件 Excel ingestion 编排：抽取 → 归一 → 颜色 tags → 入库。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
