"""parse 段：n8n workflow（dict / JSON / YAML 文本）→ 校验过的 N8nWorkflow。

pydantic 只在【这一段】出现（边界校验），convert 段不依赖 pydantic。PyYAML 在
loader.py 内延迟 import，故 `import ragspine.n8n.parse` 本身不拉 PyYAML。

Submodules:
    loader.py — JSON/YAML 装载 + 结构/connections 引用校验，吐 N8nWorkflow；另含
                load_dify_document（dify_to_n8n 入参侧的 DifyDoc 只读校验）。
    schema.py — pydantic v2 边界模型（N8nNode / N8nWorkflow），extra='allow'。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
