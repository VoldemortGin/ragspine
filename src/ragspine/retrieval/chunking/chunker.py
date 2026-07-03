"""Chunker 缝：可插拔切块策略（落地 docs/prd-breadth-via-adapters.md 的 Chunker Protocol 行）。

把现有 chunk_document 切块器抬升为一个 @runtime_checkable Chunker Protocol。默认实现
DefaultChunker 零三方依赖、确定性，行为【逐位等价】于 chunk_document——薄壳委托，零行为变化
（chunk_document 入口与签名原样保留，现有所有调用方零改动）。语义 / 上下文 / parent-child 等
质量敏感的切块策略可作为新实现注册进来而不改任何调用点。

每个注册实现都跑 tests/conformance 的 Chunker provenance 包：每个 Chunk 必带非空
source_doc_id（= doc_id，血缘根）与 locator（source_locator，citation 回指），丢血缘的实现
直接 CI 红、不进生产。
"""

import os
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from ragspine.retrieval.chunking.chunking import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    Chunk,
    DocumentMeta,
    chunk_document,
)

# 工厂读取的环境变量名（缺省 spec 时生效；范式同 store.VECTOR_STORE_ENV）。
CHUNKER_ENV = "RAGSPINE_CHUNKER"

# 第三方切块器自动发现的 entry-point group：一个包在此 group 下注册一行
# （pyproject `[project.entry-points."ragspine.chunkers"]`），make_chunker 就能按名字选中它——
# 核心零改动、零 SDK import（范式同 store.VECTOR_STORE_ENTRY_POINT_GROUP）。
CHUNKER_ENTRY_POINT_GROUP = "ragspine.chunkers"


@runtime_checkable
class Chunker(Protocol):
    """切块器缝的最小结构接口：文档级纯文本 + 元数据 -> Chunk 列表。

    每块承载 doc_id（血缘根）+ source_locator（citation 回指）。core 只 import 这个 Protocol；
    具体策略的依赖（若有，如 tokenizer / embedding）留在各实现里延迟加载，不进核心 Protocol。
    """

    def chunk(
        self,
        text: str,
        meta: DocumentMeta,
        *,
        max_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    ) -> list[Chunk]: ...


class DefaultChunker:
    """零依赖、确定性的默认切块器：薄壳委托给 chunk_document（逐位等价，零行为变化）。

    段落贪心聚合到字符预算 + 相邻块重叠 + 超长段句切/硬切，规则与口径全在 chunk_document，
    本类只把它包装成 Chunker 实例，使「切块策略」成为可替换的缝。
    """

    def chunk(
        self,
        text: str,
        meta: DocumentMeta,
        *,
        max_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    ) -> list[Chunk]:
        return chunk_document(
            text, meta, max_chars=max_chars, overlap_chars=overlap_chars
        )


# ---------------------------------------------------------------------------
# 注册表：内置切块器名字 -> 惰性 loader（返回 Chunker【类】，尚不实例化）。范式同 store.py：
# 核心 import 本模块零 SDK；第三方切块器【不】登记此表，而是经 entry-point 自动发现，无需核心 PR。
# 别名共指同一 loader（大小写 / 留白由 make_chunker 归一化）；'recursive' / 'structural' 是默认
# 段落贪心切块器的别名（能力矩阵口径）。
# ---------------------------------------------------------------------------
def _load_default() -> type[Chunker]:
    return DefaultChunker


def _load_layout() -> type[Chunker]:
    """惰性加载布局感知 + 父子切块器（W4b）；import 留在此处以保持本模块零额外依赖。"""
    from ragspine.retrieval.chunking.layout_chunker import LayoutAwareChunker

    return LayoutAwareChunker


def _load_sentence_window() -> type[Chunker]:
    """惰性加载句子窗口切块器（W10）；import 留在此处以保持本模块零额外依赖。"""
    from ragspine.retrieval.chunking.sentence_window_chunker import (
        SentenceWindowChunker,
    )

    return SentenceWindowChunker


def _load_semantic() -> type[Chunker]:
    """惰性加载语义切块器（W10）；import 留在此处（其默认 embedder 亦延迟构造）。"""
    from ragspine.retrieval.chunking.semantic_chunker import SemanticChunker

    return SemanticChunker


_BUILTIN_LOADERS: dict[str, Callable[[], type[Chunker]]] = {
    "default": _load_default,
    "recursive": _load_default,
    "structural": _load_default,
    "layout": _load_layout,
    "parent_child": _load_layout,
    "parent-child": _load_layout,
    "sentence_window": _load_sentence_window,
    "sentence-window": _load_sentence_window,
    "semantic": _load_semantic,
}

# 错误信息中展示的内置规范名（别名不重复列出，保持可读）。
_BUILTIN_DISPLAY_NAMES = ("none", "default", "layout", "sentence_window", "semantic")


def _discover_entry_points() -> Sequence[Any]:
    """发现第三方在 CHUNKER_ENTRY_POINT_GROUP 下注册的 Chunker 实现。

    返回若干 EntryPoint（各有 .name 与 .load()）。在函数内 import entry_points，使
    monkeypatch importlib.metadata.entry_points 在测试中生效，也让发现成本只在真正回落时付出。
    """
    from importlib.metadata import entry_points

    return list(entry_points(group=CHUNKER_ENTRY_POINT_GROUP))


def _resolve_factory(normalized: str) -> Callable[..., Chunker]:
    """归一化后的名字 -> 一个可 **kwargs 调用得到 Chunker 的工厂（内置类或 entry-point 目标）。

    先查内置注册表（内置名字优先于同名 entry point，第三方不能劫持内置语义）；未命中再回落到
    entry-point 自动发现，按名字（大小写 / 留白不敏感）匹配后 .load()。两者皆不命中 -> ValueError，
    列出内置 + 已发现的 entry-point 名字。本函数只【解析】不【实例化】。
    """
    loader = _BUILTIN_LOADERS.get(normalized)
    if loader is not None:
        return loader()
    discovered = _discover_entry_points()
    for entry_point in discovered:
        if entry_point.name.strip().lower() == normalized:
            factory: Callable[..., Chunker] = entry_point.load()
            return factory
    names = sorted({entry_point.name for entry_point in discovered})
    raise ValueError(
        f"未知 chunker spec：{normalized!r}"
        f"（内置可选 {' / '.join(_BUILTIN_DISPLAY_NAMES)}；"
        f"已发现的 entry-point 后端：{names or '无'}；"
        f"第三方包可在 entry-point group {CHUNKER_ENTRY_POINT_GROUP!r} 下注册一个切块器）"
    )


def make_chunker(spec: str | None = None, **kwargs: Any) -> Chunker | None:
    """切块器工厂：把「用哪个切块策略」从改代码降为一个 spec/env（范式同 make_vector_store）。

    spec 取值（大小写 / 留白不敏感；缺省读环境变量 RAGSPINE_CHUNKER）：
        - None / 'none'                       -> None（不注入具体 chunker；调用方回退到内置
          chunk_document / DefaultChunker 默认）。
        - 'default' / 'recursive' / 'structural' -> DefaultChunker（零依赖确定性默认）。
        - 其余                                -> entry-point 自动发现（第三方包在
          CHUNKER_ENTRY_POINT_GROUP 下注册即可被选中）；都不命中 -> ValueError 列出可选名字。

    名字经注册表解析（内置 loader 或 entry point），再以 **kwargs 实例化。返回 Chunker 实例或 None。
    """
    if spec is None:
        spec = os.environ.get(CHUNKER_ENV)
    normalized = (spec or "none").strip().lower()
    if normalized == "none":
        return None
    factory = _resolve_factory(normalized)
    return factory(**kwargs)
