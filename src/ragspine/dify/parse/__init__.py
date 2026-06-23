"""parse 段：Dify `.yml` 文本 → 校验过的 DifyDoc（pydantic v2 边界，extra='allow'）。

pydantic 只在【这一段】出现（边界校验），IR 与 codegen 段不依赖 pydantic。PyYAML 在
loader.py 内延迟 import（[dify] extra），故 `import ragspine.dify.parse` 本身不拉 PyYAML。

Submodules:
    loader.py — yaml.safe_load + 顶层结构/app.mode 校验，吐 DifyDoc。
    schema.py — pydantic v2 边界模型（DifyDoc / Graph / Node / Edge），extra='allow'。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
