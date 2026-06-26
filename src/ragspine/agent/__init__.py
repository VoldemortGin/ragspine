"""agent 编排层：意图解析 → 澄清网关 → 安全门 → 三路分流 → tool-use 循环 → 合成回答。

反幻觉与确定性安全门在此落地：结构化通路无 found 事实时改写为「未找到」，
SecurityGate 不可插拔、独立于意图解析器在原始问题上判定拒答。

Submodules:
    agent.py — orchestrator：唯一公开入口 answer_question()，三路分流并施加各项 guard。
    decompose.py — opt-in LLM 查询分解（W6a）：真多跳子问题拆解；默认 none 时旁路、字节不变。
    intent.py — 规则式（无 LLM）意图&范围解析 + 澄清网关。
    llm_provider.py — LLMProvider 协议 + AnthropicProvider（真实）+ MockProvider（离线确定性）。
    query_tools.py — 参数化 query_metric 查询工具：found/not_found/unrecognized，绝不臆造。
    security_gate.py — 确定性安全门（ADR 0010）：越权/竞品拒答 + 命中遮蔽。
"""

from ragspine import _lazy_submodules

_submodule_getattr, __dir__ = _lazy_submodules(__name__, __path__)


def __getattr__(name: str) -> object:
    # 显式 re-export ProviderError：lazy __getattr__ 只解析子模块、不解析属性，故
    # `ragspine.agent.ProviderError` 本不可达——这里补一条使其可达（不急切 import llm_provider）。
    if name == "ProviderError":
        import importlib

        return importlib.import_module(f"{__name__}.llm_provider").ProviderError
    return _submodule_getattr(name)
