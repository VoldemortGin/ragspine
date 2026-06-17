"""agent 编排层：意图解析 → 澄清网关 → 安全门 → 三路分流 → tool-use 循环 → 合成回答。

反幻觉与确定性安全门在此落地：结构化通路无 found 事实时改写为「未找到」，
SecurityGate 不可插拔、独立于意图解析器在原始问题上判定拒答。

Submodules:
    agent.py — orchestrator：唯一公开入口 answer_question()，三路分流并施加各项 guard。
    intent.py — 规则式（无 LLM）意图&范围解析 + 澄清网关。
    llm_provider.py — LLMProvider 协议 + AnthropicProvider（真实）+ MockProvider（离线确定性）。
    query_tools.py — 参数化 query_metric 查询工具：found/not_found/unrecognized，绝不臆造。
    security_gate.py — 确定性安全门（ADR 0010）：越权/竞品拒答 + 命中遮蔽。
"""
