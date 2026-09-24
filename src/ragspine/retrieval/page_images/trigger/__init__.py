"""trigger —— 页图按需附图（ADR 0025）：按页标签决定前 N 页里哪几页真的附图，有上限、页内去重。

``RAGSPINE_PAGE_IMAGES=off|tagged|all``（``on`` 是 ``all`` 的别名）。``all`` 且不设上限时直接用
``PageImageRetriever``（与原来的 ``on`` 逐字节一致）；其余情况在它外面包一层，**只删不增**：只去掉不满足条件的
``page_image`` 键，不新增引用，所以 RESTRICTED 出口的筛查原样继承。

Submodules:
    tag_store.py — page_tag 表（每页原始度量 + md_sha256/tags_version 签名，首次写入才建表）；旧库按叙事台账懒算、只读缓存。
    retriever.py — 模式 / 触发条件解析、PageImageTriggerRetriever（标签过滤、去重、上限截断，trace 只记计数）与装配工厂。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
