"""FAQ 短路缓存的可执行规格（TDD：先红后绿）。

测试只验证外部行为：命中/未命中语义、排除规则、有效期/启用门、敏感度门、
provenance（id/version/source）以及 from_file 往返。固定 reference_date 保证确定性。

排除规则是高风险层：错误命中会绕过系统的防编造与拒答保证，故对结构化数字问题、
外部/竞品实体问题、实时/时效问题、过期/禁用/受限条目逐项断言 MISS。
"""

import json
from datetime import date

import pytest

from ragspine.service.faq.faq_cache import FAQCache, FAQHit, FAQItem

REF = date(2026, 6, 16)


def _cache(*items: FAQItem) -> FAQCache:
    return FAQCache(items)


# --- 基础命中 -----------------------------------------------------------------

def test_exact_question_hit_returns_provenance():
    item = FAQItem(
        id="faq-1",
        question="RAGSpine 是什么？",
        answer="它是为高管做的经营洞察助手。",
        source="handbook#intro",
        version=3,
    )
    hit = _cache(item).lookup("RAGSpine 是什么？", reference_date=REF)
    assert isinstance(hit, FAQHit)
    assert hit.item_id == "faq-1"
    assert hit.version == 3
    assert hit.answer == "它是为高管做的经营洞察助手。"
    assert hit.source == "handbook#intro"
    assert hit.cache_type == "faq"


def test_alias_hit():
    item = FAQItem(
        id="faq-2",
        question="如何重置密码",
        answer="点击登录页的忘记密码。",
        aliases=("怎么重置密码", "密码忘了怎么办"),
        source="kb#42",
    )
    cache = _cache(item)
    assert cache.lookup("怎么重置密码", reference_date=REF).item_id == "faq-2"
    assert cache.lookup("密码忘了怎么办", reference_date=REF).item_id == "faq-2"


def test_normalization_whitespace_case_and_trailing_punct():
    item = FAQItem(id="faq-3", question="RAGSpine 是什么", answer="A")
    cache = _cache(item)
    # 大小写折叠 + 多余空白折叠 + 尾随标点剥离 + 全角问号
    assert cache.lookup("  ragspine   是什么？？ ", reference_date=REF) is not None
    assert cache.lookup("RAGSPINE 是什么!", reference_date=REF) is not None


def test_miss_when_no_match_returns_none():
    item = FAQItem(id="faq-1", question="RAGSpine 是什么", answer="A")
    assert _cache(item).lookup("如何报销差旅", reference_date=REF) is None


def test_empty_cache_misses():
    assert FAQCache.empty().lookup("任何问题", reference_date=REF) is None


# --- 排除规则（即使存在文本匹配条目也必须 MISS） -----------------------------

def test_exclude_structured_numeric_question():
    # 文本完全匹配，但这是结构化数字查询 → 必须交回 fact table，不得短路。
    item = FAQItem(id="bad", question="香港去年REVENUE多少", answer="伪造的 1702")
    assert _cache(item).lookup("香港去年REVENUE多少", reference_date=REF) is None


def test_exclude_external_competitor_entity():
    q = "竞安今年表现怎么样"
    # 经 intent 验证此问法触发外部实体/越权
    from ragspine.agent.intent import parse_intent, clarify_scope
    from ragspine.agent.intent import CLARIFY_OUT_OF_SCOPE_ENTITY

    intent = parse_intent(q, reference_date=REF)
    assert intent.external_entity is not None
    assert clarify_scope(intent, reference_date=REF).mode == CLARIFY_OUT_OF_SCOPE_ENTITY

    item = FAQItem(id="bad", question=q, answer="不该命中")
    assert _cache(item).lookup(q, reference_date=REF) is None


def test_exclude_realtime_temporal_cue():
    # 纯叙事问法但含"最新" → 时效线索，不得短路缓存的旧答案。
    item = FAQItem(id="bad", question="ACME最新动态", answer="旧动态")
    assert _cache(item).lookup("ACME最新动态", reference_date=REF) is None
    # "当前/今天/现在" 等同样排除
    item2 = FAQItem(id="bad2", question="当前监管要求是什么", answer="旧要求")
    assert _cache(item2).lookup("当前监管要求是什么", reference_date=REF) is None


def test_exclude_expired_item():
    item = FAQItem(
        id="exp",
        question="某政策口径",
        answer="旧政策",
        valid_until="2026-01-01",
    )
    assert _cache(item).lookup("某政策口径", reference_date=REF) is None


def test_exclude_not_yet_valid_item():
    item = FAQItem(
        id="future",
        question="某政策口径",
        answer="未来政策",
        valid_from="2027-01-01",
    )
    assert _cache(item).lookup("某政策口径", reference_date=REF) is None


def test_within_validity_window_hits():
    item = FAQItem(
        id="ok",
        question="某政策口径",
        answer="现行政策",
        valid_from="2026-01-01",
        valid_until="2026-12-31",
    )
    assert _cache(item).lookup("某政策口径", reference_date=REF).item_id == "ok"


def test_exclude_disabled_item():
    item = FAQItem(id="off", question="RAGSpine 是什么", answer="A", enabled=False)
    assert _cache(item).lookup("RAGSpine 是什么", reference_date=REF) is None


def test_exclude_restricted_sensitivity():
    item = FAQItem(
        id="r",
        question="RAGSpine 是什么",
        answer="机密",
        sensitivity="RESTRICTED",
    )
    assert _cache(item).lookup("RAGSpine 是什么", reference_date=REF) is None


def test_internal_sensitivity_default_hits():
    item = FAQItem(id="i", question="RAGSpine 是什么", answer="A")
    assert item.sensitivity == "INTERNAL"
    assert _cache(item).lookup("RAGSpine 是什么", reference_date=REF) is not None


def test_exclude_restricted_sensitivity_is_case_insensitive():
    """RESTRICTED 门必须大小写无关：小写 / 混合 / 带首尾空白的 'restricted' 同样
    绝不得短路——否则一条手写 JSON 里 sensitivity='restricted' 的机密条目会泄露。"""
    for sens in ("restricted", "Restricted", " RESTRICTED "):
        item = FAQItem(
            id="r", question="RAGSpine 是什么", answer="机密", sensitivity=sens
        )
        assert _cache(item).lookup("RAGSpine 是什么", reference_date=REF) is None, (
            f"sensitivity={sens!r} 漏短路 RESTRICTED 内容"
        )


def test_exclude_realtime_cue_is_nfkc_symmetric():
    """实时线索排除须与索引匹配同口径归一（NFKC）：全角实时词（'ｃｕｒｒｅｎｔ'）也要
    排除——否则全角实时问句会绕过排除、被短路成陈旧缓存答案。"""
    item = FAQItem(id="bad3", question="ｃｕｒｒｅｎｔ 产品愿景", answer="旧愿景")
    assert _cache(item).lookup("ｃｕｒｒｅｎｔ 产品愿景", reference_date=REF) is None


# --- 纯函数性：lookup 不触达 provider/store/retriever -------------------------

def test_lookup_is_pure_no_side_channels(monkeypatch):
    import ragspine.service.faq.faq_cache as mod

    # lookup 只允许调用 parse_intent/clarify_scope；不得引用 provider/store/retriever。
    src = (
        __import__("pathlib").Path(mod.__file__).read_text(encoding="utf-8")
    )
    for forbidden in ("answer_question", "FactStore", "MockProvider", "Retriever"):
        assert forbidden not in src


# --- from_file 往返 -----------------------------------------------------------

def test_from_file_roundtrip_items_key(tmp_path):
    p = tmp_path / "faq.json"
    p.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "f1",
                        "question": "RAGSpine 是什么",
                        "answer": "A",
                        "aliases": ["它是什么", "这是什么系统"],
                        "source": "kb#1",
                        "version": 2,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cache = FAQCache.from_file(p)
    hit = cache.lookup("它是什么", reference_date=REF)
    assert hit is not None
    assert hit.item_id == "f1"
    assert hit.version == 2
    assert hit.source == "kb#1"


def test_from_file_top_level_list(tmp_path):
    p = tmp_path / "faq.json"
    p.write_text(
        json.dumps(
            [{"id": "f1", "question": "你好", "answer": "hi"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cache = FAQCache.from_file(p)
    assert cache.lookup("你好", reference_date=REF).item_id == "f1"


def test_from_file_coerces_aliases_to_tuple(tmp_path):
    p = tmp_path / "faq.json"
    p.write_text(
        json.dumps(
            [{"id": "f1", "question": "q", "answer": "a", "aliases": ["x", "y"]}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cache = FAQCache.from_file(p)
    assert cache.lookup("x", reference_date=REF).item_id == "f1"
    # FAQItem 冻结 → aliases 必须是 tuple
    with pytest.raises(Exception):
        FAQItem(id="z", question="q", answer="a").aliases.append("nope")  # type: ignore[attr-defined]
