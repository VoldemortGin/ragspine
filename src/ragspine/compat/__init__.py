"""第三方 RAG 框架的形状适配器（opt-in，薄，绝不进默认路径）。

每个子模块克隆一家外部框架的公开 API 形状，让既有调用方零改动迁到 RAGSpine 上。纪律：
- **薄**：只做签名翻译，绝不在此重写检索/编排逻辑——一律回调既有 facade / workflow。
- **诚实**：语义对不齐处在 docstring 明说，绝不假装等价。
- **不变量优先**：外部签名若会吞掉血缘（如 LightRAG 的 `query()` 返回裸字符串），
  额外提供保住来源的出口，绝不为了迎合签名而丢掉溯源。

Submodules:
    lightrag.py — LightRAG（HKUDS）公开 API 形状克隆：insert / query / QueryParam 五档 mode。
    graphrag.py — MS GraphRAG parquet 工件双向互通：import/export entities+relationships。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
