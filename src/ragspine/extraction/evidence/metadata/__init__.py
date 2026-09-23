"""extraction.evidence.metadata —— 零模型的确定性元数据派生:页,期间,文档与目录树(ADR 0013/0019)。

Submodules:
    document_metadata.py — 由已校验的页元数据确定性折叠出的文档级元数据。
    document_tree.py — 由页元数据确定性折叠出的文档目录树。
    page_metadata.py — 页级元数据:每个值都逐字取自本页。
    periods.py — 确定性的期间规范化:一个印刷期间标签对应一个可比较形式。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
