"""answer_question 折叠：识别「问答骨架」并整体折叠成一次 ragspine.answer_question 调用。

高阶「理解」优化（P7 任务 2）：Dify 里手拼的 RAG 问答骨架——
`start → knowledge-retrieval(kr) → llm(context_ref 指向 kr) → answer/end`——在 ragspine 里有
更短更对的等价物：`answer_question(question, store, provider, narrative_retriever=...)`，自带
反幻觉（结构化无果改写「未找到」）与 provenance（sources）。本模块靠 IR 结构模式匹配把这一骨架
识别出来、产一个 FoldPlan，由 emitter 据此发一次折叠调用，替代逐节点的 retrieve+chat 手拼。

纯结构匹配，零 API、零 provider 调用。判据（全部命中才折叠）：
1. 存在一个 LLMNode，其 context_ref 非 None 且指向某 KnowledgeRetrievalNode（kr）。
2. kr 的查询来源可定位（VarRef / 模板里的单一 VarRef / 字面量皆可作为 question）。
3. 该 LLMNode 的输出有唯一下游 sink（AnswerNode 或 EndNode），即问答终点。
匹配是局部的：图里其它结构原样逐节点生成，只把命中的 (kr, llm) 对折叠。
"""

from __future__ import annotations

from dataclasses import dataclass

from ragspine.dify.ir.model import (
    AnswerNode,
    EndNode,
    IRNode,
    KnowledgeRetrievalNode,
    Literal,
    LLMNode,
    TemplateValue,
    Value,
    VarRef,
    WorkflowIR,
)


@dataclass(frozen=True)
class FoldPlan:
    """一个命中的问答折叠：把 (kr, llm) 折叠成一次 answer_question，结果落到 llm 的输出字段。

    kr_id / llm_id：被折叠的两个节点 id（emitter 见到它们时改发折叠调用、不再逐节点生成）。
    question：作为 answer_question 第一参的查询取值（kr 的 query）。
    answer_field：折叠结果写回 _ctx 的字段——沿用 llm 的输出字段名 'text'，使下游引用零改动。
    dataset_ids：kr 的数据集（注释提示，沿用 KNOWLEDGE_CHUNK_DB 接真实库）。
    """

    kr_id: str
    llm_id: str
    question: Value
    answer_field: str = "text"
    dataset_ids: tuple[str, ...] = ()


def detect_answer_question_folds(ir: WorkflowIR) -> tuple[FoldPlan, ...]:
    """扫描 IR，返回全部可折叠的问答骨架（无命中则空）。纯结构匹配，确定性。"""
    node_map = ir.graph.node_map()
    plans: list[FoldPlan] = []
    folded_kr: set[str] = set()
    for nid in ir.topo_order:  # 拓扑序保证确定性
        node = node_map.get(nid)
        if not isinstance(node, LLMNode) or node.context_ref is None:
            continue
        kr = node_map.get(node.context_ref.node_id)
        if not isinstance(kr, KnowledgeRetrievalNode):
            continue
        if kr.id in folded_kr:
            continue  # 一个 kr 只折叠进一个 llm（多消费者不折叠，保守）
        if not _has_qa_sink(ir, node.id, node_map):
            continue
        question = _question_value(kr)
        if question is None:
            continue
        folded_kr.add(kr.id)
        plans.append(FoldPlan(
            kr_id=kr.id, llm_id=node.id, question=question,
            answer_field="text", dataset_ids=kr.dataset_ids,
        ))
    return tuple(plans)


def _has_qa_sink(
    ir: WorkflowIR, llm_id: str, node_map: dict[str, IRNode]
) -> bool:
    """llm 的下游里存在 AnswerNode/EndNode（问答终点）即认定为问答骨架。"""
    for edge in ir.graph.successors(llm_id):
        target = node_map.get(edge.target)
        if isinstance(target, (AnswerNode, EndNode)):
            return True
    return False


def _question_value(kr: KnowledgeRetrievalNode) -> Value | None:
    """从 kr 的 query 取一个可作 answer_question 第一参的取值。

    VarRef / 非空 Literal 直接用；模板含单一 VarRef 时取该 VarRef；空/不可定位 → None（不折叠）。
    """
    q = kr.query
    if isinstance(q, VarRef):
        return q
    if isinstance(q, Literal):
        return q if q.value not in (None, "") else None
    if isinstance(q, TemplateValue):
        refs = q.refs()
        if len(refs) == 1:
            return refs[0]
        if q.parts:
            return q  # 多片段模板：整体作为问题文本
    return None
