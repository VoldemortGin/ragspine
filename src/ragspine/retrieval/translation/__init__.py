"""跨语言查询翻译（RAGSPINE_QUERY_TRANSLATION=off|auto）：确定性语言检测 + 译文只进检索。

服务 / facade / ``build_narrative_retriever`` 默认 ``auto``；底层 ``NarrativeIndex`` 构造默认不翻译（字节不变）。

Submodules:
    language.py   — 确定性语言检测：CJK 字符数 vs 拉丁词数（zh / en / und）；块语言优先取一致的元数据。
    translator.py — 开关解析 + QueryTranslator 缝 + LLMQueryTranslator（按问题缓存、降级原因码）+ trace。
"""

from ragspine.retrieval.translation.language import (
    LANG_EN,
    LANG_UNKNOWN,
    LANG_ZH,
    corpus_language,
    detect_language,
)
from ragspine.retrieval.translation.translator import (
    QUERY_TRANSLATION_AUTO,
    QUERY_TRANSLATION_MODES,
    QUERY_TRANSLATION_OFF,
    QUERY_TRANSLATION_PROMPT_PREFIX,
    LLMQueryTranslator,
    QueryTranslator,
    TranslationResult,
    make_query_translation_mode,
    translated_queries,
)

__all__ = [
    "LANG_EN",
    "LANG_UNKNOWN",
    "LANG_ZH",
    "QUERY_TRANSLATION_AUTO",
    "QUERY_TRANSLATION_MODES",
    "QUERY_TRANSLATION_OFF",
    "QUERY_TRANSLATION_PROMPT_PREFIX",
    "LLMQueryTranslator",
    "QueryTranslator",
    "TranslationResult",
    "corpus_language",
    "detect_language",
    "make_query_translation_mode",
    "translated_queries",
]
