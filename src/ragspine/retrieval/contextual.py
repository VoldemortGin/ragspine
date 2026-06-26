"""Contextual retrieval（W4a）：嵌入/索引前给 chunk 文本拼一个【确定性】情境头。

Anthropic「contextual retrieval」的零编造变体：情境头只取 chunk 既有的受控词表元数据
（title / entity / period / heading），**绝不调用 LLM、绝不臆造**——情境是元数据，永不可
被引为 fact。拼成的「索引文本」喂给 BM25 分词与向量嵌入，让检索能命中情境；而 chunk.text
（citation 回指的原文、「块文本 = 原文子串」的 provenance 契约）**原样不动**——情境头只活在
「索引文本」这一层，不进 source_locator、不污染被引用的原文、不破 byte-identity。

opt-in 缝：HybridRetriever / NarrativeIndex 默认 index_text_fn=None（索引文本即 chunk.text，
逐位等价旧行为）；注入 contextual_index_text 才启用。是否启用经 make_index_text_fn 由
spec/env（RAGSPINE_CONTEXTUAL）选用，范式同 retrieval.chunking.chunker.make_chunker。

LLM 情境适配器（follow-up，behind [llm]）：一个 LLM 写的 per-chunk 情境 blurb——higher-recall、
默认关。它只是另一个 IndexTextFn 实现，经同一 index_text_fn 缝注入即可，core 零改动；本轮只留缝。
"""

import os
from collections.abc import Callable
from typing import Any

# 缺省 spec 时工厂读取的环境变量名（范式同 chunker.CHUNKER_ENV）。
CONTEXTUAL_ENV = "RAGSPINE_CONTEXTUAL"

# 索引文本函数：chunk -> 喂给 BM25 分词 / 向量嵌入的文本（None 时调用方回退 chunk.text）。
IndexTextFn = Callable[[Any], str]

# 情境头取的受控词表字段（按此顺序拼接），标签固定、零编造。
_HEADER_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "文档"),
    ("entity", "实体"),
    ("period", "期间"),
    ("heading", "章节"),
)

# 工厂可识别的「启用」别名（大小写 / 留白不敏感）。
_ON_ALIASES = frozenset({"default", "deterministic", "on", "contextual"})


def build_context_header(chunk: Any) -> str:
    """从 chunk 既有受控元数据拼确定性情境头；无任何可用字段 -> 空串。

    形如 '[文档:2025上半年财务 · 实体:ACME_HK · 期间:2025H1]'。只收非空字段，顺序固定
    （确定性）；值全部来自 chunk 元数据（getattr 容缺，StoredChunk 无 heading 列也安全），
    绝不臆造。
    """
    parts: list[str] = []
    for attr, label in _HEADER_FIELDS:
        value = getattr(chunk, attr, "")
        if value:
            parts.append(f"{label}:{value}")
    if not parts:
        return ""
    return "[" + " · ".join(parts) + "]"


def contextual_index_text(chunk: Any) -> str:
    """chunk 的【索引文本】= 情境头 + 换行 + 原文；无头则原样返回 chunk.text。

    chunk.text（citation 原文）不被改动——头只活在返回值里。这是默认的确定性 IndexTextFn。
    """
    header = build_context_header(chunk)
    text: str = chunk.text
    return f"{header}\n{text}" if header else text


def make_index_text_fn(spec: str | None = None) -> IndexTextFn | None:
    """索引文本策略工厂（范式同 make_chunker）：把「是否启用 contextual」降为一个 spec/env。

    spec 取值（大小写 / 留白不敏感；缺省读环境变量 RAGSPINE_CONTEXTUAL）：
        - None / 'none'                           -> None（调用方回退 chunk.text，逐位等价旧行为）。
        - 'default'/'deterministic'/'on'/'contextual' -> contextual_index_text（确定性情境头）。
        - 其余                                    -> ValueError 列出可选名字。
    """
    if spec is None:
        spec = os.environ.get(CONTEXTUAL_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized == "none":
        return None
    if normalized in _ON_ALIASES:
        return contextual_index_text
    raise ValueError(
        f"未知 contextual spec：{normalized!r}"
        f"（可选 none / {' / '.join(sorted(_ON_ALIASES))}）"
    )
