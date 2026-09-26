"""W12-B 多路检索融合：把【OCR→text 文本通道】与【ColPali 视觉通道】按 RRF 合一（opt-in）。

现状（docs/prd-quality-depth.md W12 follow-up）：W3a OCR→text 与 W12 ColPali 视觉是【并列】两条路线——
扫描/图表密集页 OCR→text 易塌结构（表格行列丢、bbox 抖动），而视觉路线保版面但需 GPU。本模块把两条
路线用 RRF（复用 W1 rrf_fuse）融合成一条统一排序：弱腿不拖累、强腿胜出，且【同一 (doc, page) 两腿
都命中】时分数相加 + 合并成一条（视觉确认 boost），文本命中优先做代表（带 text，LLM 可消费）。

设计（守 ADR 0001 确定性 + 反编造 + 隔离）：
- **opt-in、默认透传**：visual_retriever=None 时 FusedRetriever.retrieve 原样返回文本检索器输出（不改形状、
  不加注，等价于不融合）——未接视觉腿即无影响。
- **隔离**：两腿本应在各自出口剔除 RESTRICTED；启用融合时再次排除受限命中，绝不直读库。
  视觉协议不支持 filters，存在过滤条件时只允许确认文本腿已经选中的明确文档页，不增加视觉页。
- **确定性**：rrf_fuse 确定 + 稳定平分兜底（首次出现序）=> 可复现（真 ColPali 推理非确定，故整链 opt-in）。
- **provenance 不动**：每条命中保留原 doc_id / source_locator / page / scores，仅【加注】fused_score 与
  retrieval_routes（与视觉确认时的 visual_score）。

融合键：能解析出 (doc_id, page) 的命中按此键跨腿对齐（两腿命中同页 => 同键 => 分数相加 + 合并）；
解析不出 page 或缺文档身份的命中按各腿位次独立计入——此时为纯 RRF 交错（仍有价值）。
"""

import re
from typing import Protocol, runtime_checkable

from ragspine.retrieval.lexical.retrieval import DEFAULT_RRF_K, rrf_fuse
from ragspine.retrieval.rerank.listwise_rerank import RESTRICTED_SENSITIVITY

# 从 source_locator 解析页号：仅认有定位分隔符的 page/slide（不认 para，避免 'para1' 误判成页）。
_PAGE_RE = re.compile(r"(?:^|[#@,;])(?:page|slide)[=#]?(\d+)(?=$|[#@,;])", re.IGNORECASE)

# 每腿默认取多少条参与融合（多于最终 top_k，给融合留重排空间）。
DEFAULT_TEXT_FETCH = 50
DEFAULT_VISUAL_FETCH = 10

__all__ = [
    "RESTRICTED_SENSITIVITY",
    "NarrativeRetriever",
    "VisualRetriever",
    "FusedRetriever",
    "make_fused_retriever",
    "DEFAULT_TEXT_FETCH",
    "DEFAULT_VISUAL_FETCH",
]


@runtime_checkable
class NarrativeRetriever(Protocol):
    """文本检索协议（duck-typed，结构等同 agent.NarrativeRetriever）。"""

    def retrieve(
        self, query: str, *, filters: dict[str, str] | None = None, top_k: int = 50
    ) -> list[dict[str, object]]: ...


@runtime_checkable
class VisualRetriever(Protocol):
    """视觉检索协议（结构等同 vision.colpali.ColPaliVisualRetriever）：无 filters。"""

    def retrieve(self, query: str, *, top_k: int = 10) -> list[dict[str, object]]: ...


def _doc_id(hit: dict[str, object]) -> str:
    value = hit.get("doc_id")
    return value if isinstance(value, str) and value.strip() else ""


def _page_of(hit: dict[str, object]) -> int | None:
    """命中页号：page_no / page 字段优先；否则从 source_locator 解析 page/slide 数字；都无则 None。"""
    for field in ("page_no", "page"):
        p = hit.get(field)
        if isinstance(p, int) and not isinstance(p, bool):
            return p
    m = _PAGE_RE.search(str(hit.get("source_locator") or ""))
    return int(m.group(1)) if m else None


def _fusion_key(hit: dict[str, object]) -> str | None:
    """只有带文档身份的明确页码可以跨腿对齐。"""
    doc_id = _doc_id(hit)
    page = _page_of(hit)
    if doc_id and page is not None:
        return repr((doc_id, page))
    return None


def _visual_score(hit: dict[str, object]) -> float | None:
    scores = hit.get("scores")
    if isinstance(scores, dict):
        v = scores.get("visual_maxsim", scores.get("colpali_maxsim"))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


class FusedRetriever:
    """RRF 融合文本通道与视觉通道（实现 NarrativeRetriever 协议）。

    text_retriever：OCR→text / narrative 文本检索器（必填）。
    visual_retriever：ColPali 视觉检索器（可选；None => 透传文本腿，等价不融合）。
    text/visual_fetch：每腿取多少条参与融合（多于最终 top_k）。rrf_k：RRF 常数。

    融合：两腿各自检索 -> 按 _fusion_key 编排成 RRF 排名 -> rrf_fuse 打分 -> 同键合并（文本命中优先做
    代表、带 text；视觉命中补 page/视觉确认）-> 按 fused_score 降序、稳定平分兜底 -> 截 top_k。
    """

    def __init__(
        self,
        text_retriever: NarrativeRetriever,
        visual_retriever: VisualRetriever | None = None,
        *,
        rrf_k: float = DEFAULT_RRF_K,
        text_fetch: int = DEFAULT_TEXT_FETCH,
        visual_fetch: int = DEFAULT_VISUAL_FETCH,
    ) -> None:
        self.text_retriever = text_retriever
        self.visual_retriever = visual_retriever
        self.rrf_k = rrf_k
        self.text_fetch = text_fetch
        self.visual_fetch = visual_fetch

    def retrieve(
        self,
        query: str,
        *,
        filters: dict[str, str] | None = None,
        top_k: int = 50,
    ) -> list[dict[str, object]]:
        if self.visual_retriever is None:
            return self.text_retriever.retrieve(query, filters=filters, top_k=top_k)
        if top_k <= 0:
            return []
        text_hits = self.text_retriever.retrieve(
            query, filters=filters, top_k=max(top_k, self.text_fetch)
        )
        text_hits = [
            hit
            for hit in text_hits
            if str(hit.get("sensitivity", "")).upper() != RESTRICTED_SENSITIVITY
        ]
        visual_hits = self.visual_retriever.retrieve(query, top_k=self.visual_fetch)
        visual_hits = [
            hit
            for hit in visual_hits
            if str(hit.get("sensitivity", "")).upper() != RESTRICTED_SENSITIVITY
        ]
        if filters:
            # 视觉协议不支持 metadata filters：仅确认文本腿已选中的明确文档页。
            allowed_pages = {key for hit in text_hits if (key := _fusion_key(hit)) is not None}
            visual_hits = [hit for hit in visual_hits if _fusion_key(hit) in allowed_pages]

        # 每腿编排成 RRF 排名（融合键序）+ 记代表命中 + 首次出现序（稳定平分兜底）。
        reps: dict[str, dict[str, object]] = {}
        routes: dict[str, set[str]] = {}
        first_seen: dict[str, int] = {}
        order = 0

        def ingest(hits: list[dict[str, object]], route: str) -> list[str]:
            nonlocal order
            ranking: list[str] = []
            seen: set[str] = set()
            for index, hit in enumerate(hits):
                key = _fusion_key(hit) or f"{route}::{index}"
                if key in seen:
                    continue
                seen.add(key)
                ranking.append(key)
                routes.setdefault(key, set()).add(route)
                if key not in reps:
                    reps[key] = dict(hit)
                    first_seen[key] = order
                    order += 1
                elif route == "visual" and not reps[key].get("is_visual"):
                    # 同键已是文本代表，视觉命中确认 -> 仅把视觉确认信息补到文本代表上。
                    vs = _visual_score(hit)
                    if vs is not None:
                        reps[key]["visual_score"] = vs
            return ranking

        rankings = [ingest(text_hits, "text"), ingest(visual_hits, "visual")]
        fused = rrf_fuse(rankings, k=self.rrf_k)

        ordered = sorted(reps, key=lambda key: (-fused.get(key, 0.0), first_seen[key]))
        out: list[dict[str, object]] = []
        for key in ordered[:top_k]:
            rep = reps[key]
            rep["fused_score"] = fused.get(key, 0.0)
            rep["retrieval_routes"] = sorted(routes[key])
            out.append(rep)
        return out


def make_fused_retriever(
    text_retriever: NarrativeRetriever,
    visual_retriever: VisualRetriever | None = None,
    *,
    rrf_k: float = DEFAULT_RRF_K,
    text_fetch: int = DEFAULT_TEXT_FETCH,
    visual_fetch: int = DEFAULT_VISUAL_FETCH,
) -> NarrativeRetriever:
    """融合检索器工厂：visual_retriever=None 返回文本腿本身（透传，等价不融合，opt-in）。

    范式同 make_corrective_retriever / make_postprocessing_retriever：未接视觉腿即无影响（默认行为不变）；
    接了才包成 FusedRetriever。rrf_k / text_fetch / visual_fetch 透传。
    """
    if visual_retriever is None:
        return text_retriever
    return FusedRetriever(
        text_retriever,
        visual_retriever,
        rrf_k=rrf_k,
        text_fetch=text_fetch,
        visual_fetch=visual_fetch,
    )
