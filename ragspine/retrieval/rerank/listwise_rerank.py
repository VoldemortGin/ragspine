"""Claude listwise 二审（精排）：协议 + prompt 构造 + 鲁棒解析 + 退化策略。

拍板（docs/architecture.md）：精排直接用 Claude listwise 二审（低 QPS 成本可承受），不默认部署
本地 reranker；Restricted 敏感层数据不出域。本模块只做协议 / prompt / 解析 / 编排，
真实 Claude provider 由集成线实现 ListwiseJudge 注入，本文件零 SDK、零网络。

Restricted 不出域策略（方案 B，理由见模块尾注）：
    候选中 sensitivity=='RESTRICTED'（大小写不敏感）的块**绝不进入发给 judge 的候选
    文本**；仅对非 Restricted 子集做 listwise 二审，Restricted 块按其原 RRF 位次原位
    保留，非 Restricted 块按 judge 排序回填其余位置。全 Restricted 时 judge 完全不被
    调用，整体退化为 RRF 序。

失败退化：judge 抛异常 / 返回非法内容 -> 一律退化为输入顺序（RRF 序），绝不抛死。

方案 B vs 方案 A（整体退化）的取舍：高管场景下 Restricted 块（PR 评级、Exco 纪要）
在候选中出现是常态而非例外，方案 A 会让大量查询完全失去精排收益；方案 B 在同样满足
「Restricted 文本零出域」硬约束（有测试钉死）的前提下保住非敏感候选的二审质量，且
原位保留使 Restricted 的相对位次仍由域内可解释的 RRF 决定。
"""

import re
from typing import Protocol, Sequence

DEFAULT_TOP_N = 10
RESTRICTED_SENSITIVITY = "RESTRICTED"

# 回文里所有非负整数都按候选下标候选处理（越界/重复在解析时容错）。
_INDEX_RE = re.compile(r"\d+")


class ListwiseJudge(Protocol):
    """listwise 精排协议（依赖注入点）。

    judge(query, candidates) 返回按相关性降序的候选下标列表（0-based）。
    真实现为 Claude（集成线负责，用 build_listwise_prompt 构造提示词、
    parse_listwise_response 解析模型回文）；测试用确定性 fake。
    """

    def judge(self, query: str, candidates: list[str]) -> list[int]:
        ...


def build_listwise_prompt(query: str, candidates: list[str]) -> str:
    """构造 listwise 排序 prompt：查询 + '[i] 候选文本' 编号清单 + 仅输出下标的指令。

    确定性纯函数；调用方须保证 candidates 已剔除 Restricted 文本。
    """
    lines = [
        "你是检索精排员。给定一个查询和若干编号候选片段，按与查询的相关性从高到低排序。",
        "只输出逗号分隔的候选下标（0-based），最相关的在最前，不要输出任何其他内容。",
        "",
        f"查询：{query}",
        "",
        "候选：",
    ]
    lines.extend(f"[{i}] {candidate}" for i, candidate in enumerate(candidates))
    lines += ["", "排序结果（仅下标，逗号分隔）："]
    return "\n".join(lines)


def parse_listwise_response(text: str, n_candidates: int) -> list[int]:
    """把模型回文鲁棒解析为候选下标的排列（长度恒等于 n_candidates）。

    容错：任意分隔/包裹格式（'2,0,1'、'[2] > [0]'、叙述文本）中按出现顺序取整数；
    重复去重保首现；越界下标丢弃；缺漏下标按升序补到尾部；完全无合法下标（乱文/空串）
    退化为恒等排列 [0..n-1]。
    """
    if n_candidates <= 0:
        return []
    order: list[int] = []
    seen: set[int] = set()
    for match in _INDEX_RE.finditer(text or ""):
        idx = int(match.group(0))
        if 0 <= idx < n_candidates and idx not in seen:
            order.append(idx)
            seen.add(idx)
    # 缺漏按升序补尾；order 为空时即恒等排列。
    order.extend(i for i in range(n_candidates) if i not in seen)
    return order


def listwise_rerank(
    query: str,
    results: Sequence,
    judge: ListwiseJudge | None,
    *,
    top_n: int = DEFAULT_TOP_N,
) -> list:
    """对混合检索结果（RRF 序）做 listwise 二审，返回 top_n。

    results 为鸭子类型：元素须有 .chunk.text 与 .chunk.sensitivity（即
    ragspine.retrieval.lexical.retrieval.RetrievalResult；不 import 它以避免环依赖）。

    - judge 为 None / results 为空 -> 直接截 top_n（保持 RRF 序）。
    - Restricted 块不送 judge、原位保留（见模块 docstring 策略）。
    - judge 异常或返回非法 -> 非 Restricted 子集退化为原序，绝不抛出。
    """
    items = list(results)
    if not items or judge is None:
        return items[:top_n]

    restricted_positions = {
        i
        for i, r in enumerate(items)
        if str(r.chunk.sensitivity).upper() == RESTRICTED_SENSITIVITY
    }
    open_items = [r for i, r in enumerate(items) if i not in restricted_positions]
    if not open_items:
        # 全 Restricted：judge 完全不被调用，整体退化为 RRF 序。
        return items[:top_n]

    identity = list(range(len(open_items)))
    try:
        raw = judge.judge(query, [r.chunk.text for r in open_items])
        order: list[int] = []
        seen: set[int] = set()
        for idx in raw:
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            if 0 <= idx < len(open_items) and idx not in seen:
                order.append(idx)
                seen.add(idx)
        # judge 漏排的候选按原（RRF）序补尾。
        order.extend(i for i in identity if i not in seen)
    except Exception:
        order = identity

    reordered = iter(open_items[i] for i in order)
    merged = [
        item if i in restricted_positions else next(reordered)
        for i, item in enumerate(items)
    ]
    return merged[:top_n]
