"""graph —— GraphRAG 域（W7）：确定性结构关系图 + GraphStore 缝 + 叙事 GraphRAG 骨架。

契合宪章的 GraphRAG，两层 + 一个缝（ADR 0001/0005）：
- W7a 结构关系图（确定性、零 LLM 抽取、零编造）：在【已有受控维度】上建图——母子 roll-up /
  派生链 / 竞品边 / doc↔实体·指标共现——解锁 flat top-k + 精确 SQL 做不到的多跳结构化查询
  （同业对比 / 子公司汇总 / 派生追溯），全程可引用（每节点/边带 source 血缘）。
- W7c GraphStore 缝（🔧 breadth 契约）：GraphStore Protocol + 零依赖确定性内存默认
  （InProcessGraphStore）+ 薄 adapter + make_graph_store 注册表 + provenance/isolation
  conformance pack（每 node/edge 带血缘；RESTRICTED 来源 node 绝不出现在 traversal）。
- W7b 叙事 GraphRAG（opt-in、默认关、behind [graph]+[llm]）：实体/关系抽取 + 社区发现 +
  社区摘要骨架。LLM 抽取非确定 → 永不在默认路径；社区摘要明确标注为合成、绝不可引为 fact。

默认 answer_question / 检索 / eval 字节不变——本域全是新增 opt-in 能力。

Submodules:
    store.py — W7c GraphStore 缝：Protocol + 零依赖确定性内存默认 InProcessGraphStore + 工厂。
    relation.py — W7a 结构关系图：从受控维度（profile + facts + chunks）确定性建图。
    query.py — W7a 多跳查询入口：子公司汇总 / 同业对比 / 派生追溯（继承安全门 + RESTRICTED 隔离）。
    narrative.py — W7b 叙事 GraphRAG 骨架（opt-in，behind [graph]+[llm]）：抽取 + 社区 + 摘要。
    adapters/ — 第三方 GraphStore 适配器（networkx 等），延迟 import，behind [graph] extra。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
