"""automatic 元数据过滤缝（批次 2.2 ①，opt-in 默认关）：从 query 抽过滤条件。

反捏造纪律（硬约束）：
    - 抽取结果【只】是一个 MetadataFilter（若干 FilterCondition），结构上无法进入答案通道——它只喂给
      打分前的收窄阶段，绝不成为答案文本 / 引用 / 事实值。
    - 过滤只收窄候选（MetadataFilter.apply 恒返回子序列），故 RESTRICTED 语义绝不被绕过。
    - 默认离线路径【不启用】自动抽取：make_filter_extractor(None) 返回 None（无缝注入）。LLM 抽取器作
      opt-in 适配器经此缝接入（follow-up）；本模块只给一个确定性、零网络的规则默认实现供 opt-in 选用。

范式同 make_reranker / make_corrective_retriever：Protocol + 确定性离线默认 + make_* 工厂 + env 选型。
"""

import os
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ragspine.common.glossary import ENTITY_SYNONYMS, METRIC_SYNONYMS
from ragspine.retrieval.filtering.metadata_filter import FilterCondition, MetadataFilter

# 选型读取的环境变量名（缺省 spec 时生效）。
FILTER_EXTRACTOR_ENV = "RAGSPINE_FILTER_EXTRACTOR"


@runtime_checkable
class FilterExtractor(Protocol):
    """过滤条件抽取缝：query -> MetadataFilter | None（None 表示未抽出任何条件，不过滤）。

    返回类型刻意收敛为 MetadataFilter——抽取产物只能是过滤条件，绝无途径进入答案通道。真实 LLM
    抽取器作 opt-in 适配器实现此协议（集成线注入）；核心只 import 协议，零 SDK。
    """

    def extract(self, query: str) -> MetadataFilter | None: ...


@dataclass
class ControlledVocabFilterExtractor:
    """确定性、零网络的规则抽取器（离线默认，opt-in）：从 query 里的受控词表词条抽等值过滤条件。

    只认 glossary 的受控词表（METRIC_SYNONYMS -> topic 代码、ENTITY_SYNONYMS -> entity 代码），命中即产
    一条 eq 条件（field=topic/entity）。零 LLM、只读复用 glossary、确定性；抽不到 -> None（不过滤）。
    刻意保守：只从封闭受控词表映射，绝不从自由文本臆测字段值（反捏造）。
    """

    combine: str = "and"

    def extract(self, query: str) -> MetadataFilter | None:
        lowered = query.lower()
        conditions: list[FilterCondition] = []
        seen: set[tuple[str, str]] = set()
        for field, vocab in (("topic", METRIC_SYNONYMS), ("entity", ENTITY_SYNONYMS)):
            for term, code in vocab.items():
                if self._contains(lowered, term) and (field, code) not in seen:
                    conditions.append(FilterCondition(field, "eq", code))
                    seen.add((field, code))
        if not conditions:
            return None
        return MetadataFilter(conditions=tuple(conditions), combine=self.combine)

    @staticmethod
    def _contains(lowered_query: str, term: str) -> bool:
        """ASCII 词条要求词边界（避免 'cn' 命中 'concern'）；CJK 词条子串匹配（口径同 GlossaryQueryRewriter）。"""
        if term.isascii():
            return re.search(rf"\b{re.escape(term)}\b", lowered_query) is not None
        return term in lowered_query


_ALIASES = {
    "keyword": ControlledVocabFilterExtractor,
    "vocab": ControlledVocabFilterExtractor,
    "controlled_vocab": ControlledVocabFilterExtractor,
    "glossary": ControlledVocabFilterExtractor,
}


def make_filter_extractor(spec: str | None = None, **kwargs: object) -> FilterExtractor | None:
    """过滤抽取器工厂：默认 'none' 返回 None（离线路径不启用自动抽取，字节不变）。

    spec 取值（大小写 / 留白 / 连字符不敏感；缺省读环境变量 RAGSPINE_FILTER_EXTRACTOR）：
        - None / 'none'                                   -> None（默认，不启用；无缝注入）。
        - 'keyword' / 'vocab' / 'controlled_vocab' / 'glossary'
                                                          -> ControlledVocabFilterExtractor（确定性、
          零网络的受控词表规则抽取器，opt-in）。
        - 其他                                             -> ValueError（列清可用 spec）。

    LLM 抽取器作进一步 opt-in 适配器经 FilterExtractor 协议注入（follow-up），本工厂不内置任何联网实现。
    """
    if spec is None:
        spec = os.environ.get(FILTER_EXTRACTOR_ENV)
    normalized = (spec or "none").strip().lower().replace("-", "_")
    if normalized == "none":
        return None
    factory = _ALIASES.get(normalized)
    if factory is None:
        raise ValueError(
            f"未知 filter_extractor spec {spec!r}；可用："
            "none（默认关） / keyword / vocab / controlled_vocab / glossary"
        )
    return factory(**kwargs)  # type: ignore[arg-type]
