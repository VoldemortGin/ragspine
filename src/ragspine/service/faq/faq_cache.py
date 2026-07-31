"""SME 审核型 FAQ 短路缓存。

高风险层：错误命中会绕过系统的防编造与拒答保证，因此排除规则前置且保守。
lookup 是纯函数——只依赖 parse_intent/clarify_scope 做边界判定，绝不触达 LLM
provider、fact store 或 retriever。第一版只做 exact/alias 精确匹配（NFKC 归一 +
大小写折叠 + 空白折叠 + 尾随标点剥离），不做 fuzzy/embedding。
"""

import json
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ragspine.agent.intent import (
    CLARIFY_OUT_OF_SCOPE_ENTITY,
    ROUTE_STRUCTURED,
    clarify_scope,
    parse_intent,
)

# 实时 / 时效线索：出现即排除短路（缓存的固定答案对这类问题会过时）。
_REALTIME_CUES = (
    "今天", "昨天", "现在", "此刻", "当前", "实时", "最新", "目前",
    "today", "yesterday", "current", "latest", "right now", "as of now",
    "股价", "price now",
)

# 归一化时剥离的尾随标点。
_TRAILING_PUNCT = "?？。.！!"


@dataclass(frozen=True)
class FAQItem:
    """一条 SME 审核过的 FAQ 条目。"""

    id: str
    question: str
    answer: str
    aliases: tuple[str, ...] = ()
    source: str | None = None
    version: int = 1
    enabled: bool = True
    valid_from: str | None = None
    valid_until: str | None = None
    sensitivity: str = "INTERNAL"
    owner: str | None = None


@dataclass(frozen=True)
class FAQHit:
    """FAQ 命中结果（带 provenance + cache metadata）。"""

    item_id: str
    version: int
    answer: str
    source: str | None
    cache_type: str = "faq"


def _normalize(text: str) -> str:
    """NFKC + casefold + 空白折叠 + 尾随标点剥离。"""
    norm = unicodedata.normalize("NFKC", text).casefold()
    norm = " ".join(norm.split())
    return norm.rstrip(_TRAILING_PUNCT).strip()


def _within_validity(item: FAQItem, ref: date) -> bool:
    """[valid_from, valid_until] 闭区间内（缺界=开放）。解析失败按不在窗口处理。"""
    try:
        if item.valid_from is not None and ref < date.fromisoformat(item.valid_from):
            return False
        if item.valid_until is not None and ref > date.fromisoformat(item.valid_until):
            return False
    except ValueError:
        return False
    return True


class FAQCache:
    """归一化精确匹配的 FAQ 短路缓存。"""

    def __init__(self, items: Iterable[FAQItem]):
        self._items: list[FAQItem] = list(items)
        # 归一化文本 → 条目（同一文本后者覆盖前者，保持构造顺序的稳定语义）。
        self._index: dict[str, FAQItem] = {}
        for item in self._items:
            if not item.enabled:
                continue
            for surface in (item.question, *item.aliases):
                key = _normalize(surface)
                if key:
                    self._index[key] = item

    @classmethod
    def empty(cls) -> "FAQCache":
        return cls([])

    @classmethod
    def from_file(cls, path: str | Path) -> "FAQCache":
        """从 JSON 加载：{"items":[...]} 或顶层 [...]；aliases list→tuple。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_items = data["items"] if isinstance(data, dict) else data
        items = [cls._item_from_dict(d) for d in raw_items]
        return cls(items)

    @staticmethod
    def _item_from_dict(d: dict[str, Any]) -> FAQItem:
        return FAQItem(
            id=str(d["id"]),
            question=str(d["question"]),
            answer=str(d["answer"]),
            aliases=tuple(d.get("aliases", ()) or ()),
            source=d.get("source"),
            version=int(d.get("version", 1)),
            enabled=bool(d.get("enabled", True)),
            valid_from=d.get("valid_from"),
            valid_until=d.get("valid_until"),
            sensitivity=str(d.get("sensitivity", "INTERNAL")),
            owner=d.get("owner"),
        )

    def lookup(
        self, question: str, *, reference_date: date | None = None
    ) -> FAQHit | None:
        """归一化精确匹配；排除规则前置（任一命中即 MISS）。纯函数，无副作用。"""
        ref = reference_date or date.today()

        # 1) 边界排除 —— 任一命中即返回 None。
        intent = parse_intent(question, reference_date=ref)
        clar = clarify_scope(intent, reference_date=ref)
        if clar.mode == CLARIFY_OUT_OF_SCOPE_ENTITY:
            return None
        if intent.external_entity is not None:
            return None
        if intent.route == ROUTE_STRUCTURED:
            return None
        if intent.metric or intent.entity or intent.period:
            return None
        if intent.metrics or intent.entities or intent.periods:
            return None
        # 实时线索扫描须与索引匹配同口径归一（NFKC + casefold + 空白折叠），否则
        # 全角/兼容形实时词（如"ｃｕｒｒｅｎｔ"）会绕过本排除、把实时问句短路成陈旧答案。
        normalized_q = _normalize(question)
        if any(cue in normalized_q for cue in _REALTIME_CUES):
            return None

        # 2-3) 归一化精确匹配（question/alias）。
        item = self._index.get(_normalize(question))
        if item is None:
            return None

        # 4) 有效期 + 启用门（构造已过滤 disabled，这里再防御一次）。
        if not item.enabled or not _within_validity(item, ref):
            return None

        # 5) 敏感度门：v1 不短路 RESTRICTED。大小写无关 + 去首尾空白——手写 JSON 里
        #    'restricted' / 'Restricted' / ' RESTRICTED ' 同样视为受限，绝不漏短路。
        if item.sensitivity.strip().upper() == "RESTRICTED":
            return None

        # 6) 命中。
        return FAQHit(
            item_id=item.id,
            version=item.version,
            answer=item.answer,
            source=item.source,
        )
