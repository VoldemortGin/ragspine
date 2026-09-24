"""ingestion —— IR/文本 → 各类存储：结构化事实入库、叙事块入库、人工复核队列。

结构化入库幂等：重跑同一批次不得重复写入，由 manifest 台账守护。

Submodules:
    narrative/ — 叙事文档抽取 + 批量切块入库。
    page_images/ — DI markdown 显式关联原 PDF：页数校验 + sha256，入库渲染页图（内容寻址，RESTRICTED 页不渲染）。
    review/ — SME 人工复核队列状态机。
    source/ — 原始文档入口缝：SourceConnector Protocol + RawDoc + 文件系统默认实现。
    structured/ — 结构化事实入库 + 幂等批量 manifest 台账。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
