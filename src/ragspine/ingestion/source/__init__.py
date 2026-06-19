"""source —— 原始文档入口缝：把「原始文档从哪进 ingestion」收敛到一个 Protocol 后面。

这条缝只担一件事：枚举原始文档（本地盘 / 将来的 S3/Drive/Notion/HTTP），产出带齐血缘的
RawDoc 流，让 provenance 不变量从【入口】就被 conformance 绑死。抽取 / 切块 / 入库在下游。

Submodules:
    connector.py — SourceConnector Protocol + RawDoc + FilesystemConnector 默认 + make_source_connector 工厂。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
