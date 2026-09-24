"""跨语言查询翻译（RAGSPINE_QUERY_TRANSLATION=auto）：语言检测、触发条件、缓存、降级计数、只进检索。"""

import logging
import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question
from ragspine.agent.llm_provider import MockProvider, _completion_text
from ragspine.retrieval.chunking.chunking import Chunk
from ragspine.retrieval.lexical.retrieval import HybridRetriever, NarrativeIndex
from ragspine.retrieval.link.narrative_link import (
    NarrativeIndexRetriever,
    build_narrative_retriever,
)
from ragspine.retrieval.translation import (
    LANG_EN,
    LANG_UNKNOWN,
    LANG_ZH,
    QUERY_TRANSLATION_AUTO,
    QUERY_TRANSLATION_OFF,
    LLMQueryTranslator,
    corpus_language,
    detect_language,
    make_query_translation_mode,
)

from .conftest import TranslatingProvider

_OP = "narrative.query_translation"
_ZH_Q = "分销渠道 占比"
_EN_Q = "Agency Partnerships share"


def _traces(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "op", "") == _OP]


def _chunk(seq: int, text: str, **kw) -> Chunk:
    return Chunk(
        chunk_id=f"d#c{seq}",
        doc_id="d",
        seq=seq,
        text=text,
        source_locator=f"d@page={seq + 1}#para1",
        para_start=1,
        para_end=1,
        **kw,
    )


# ---------------------------------------------------------------- language detection


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026 上半年 分销渠道 占比", LANG_ZH),
        ("代理人科技投入的三个阶段分别是什么？", LANG_ZH),
        ("泰国 1H26 VONB", LANG_ZH),
        ("Thailand 1H26 VONB", LANG_EN),
        ("What was AIA's record Operating ROE in 1H 2026?", LANG_EN),
        ("VONB 1H26 占比", LANG_ZH),
        ("2026 1H26 ？", LANG_UNKNOWN),
        ("", LANG_UNKNOWN),
    ],
)
def test_detect_language(text, expected):
    assert detect_language(text) == expected


def test_detect_language_is_deterministic():
    assert {detect_language("新加坡 VONB") for _ in range(20)} == {LANG_ZH}


def test_corpus_language_from_text_statistics():
    english = [
        _chunk(0, "Agency share of VONB rose."),
        _chunk(1, "新加坡 note in passing text here"),
    ]
    chinese = [_chunk(0, "香港 2024 年收入为 VONB 100。"), _chunk(1, "新业务价值增长")]
    assert corpus_language(english) == LANG_EN
    assert corpus_language(chinese) == LANG_ZH
    assert corpus_language([]) == LANG_UNKNOWN


def test_corpus_language_prefers_uniform_metadata():
    chunks = [_chunk(0, "Agency share", language="zh-CN"), _chunk(1, "VONB", language="zh")]
    assert corpus_language(chunks) == LANG_ZH
    mixed = [_chunk(0, "Agency share", language="zh"), _chunk(1, "VONB mix", language="")]
    assert corpus_language(mixed) == LANG_EN


def test_mode_parsing():
    assert make_query_translation_mode(None) == QUERY_TRANSLATION_OFF
    assert make_query_translation_mode(" AUTO ") == QUERY_TRANSLATION_AUTO
    assert make_query_translation_mode("none") == QUERY_TRANSLATION_OFF
    with pytest.raises(ValueError):
        make_query_translation_mode("always")


# ---------------------------------------------------------------- translator: cache / fallback


def test_translator_caches_per_question_and_target():
    provider = TranslatingProvider({_ZH_Q: _EN_Q})
    translator = LLMQueryTranslator(provider)
    first = translator.translate(_ZH_Q, target_language=LANG_EN)
    second = translator.translate(_ZH_Q, target_language=LANG_EN)
    assert first.text == second.text == _EN_Q
    assert (first.cache_hit, second.cache_hit) == (False, True)
    assert len(provider.calls) == 1
    system = provider.calls[0][0]
    assert system["role"] == "system" and "English" in system["content"]
    assert provider.calls[0][-1] == {"role": "user", "content": _ZH_Q}


@pytest.mark.parametrize(
    ("provider", "reason"),
    [
        (None, "no_provider"),
        (TranslatingProvider(fail=True), "provider_error"),
        (TranslatingProvider({}), "empty_output"),
        (TranslatingProvider({_ZH_Q: _ZH_Q}), "unchanged"),
        (TranslatingProvider({_ZH_Q: "分销渠道占比"}), "wrong_language"),
    ],
)
def test_translator_fallback_reasons(provider, reason):
    result = LLMQueryTranslator(provider).translate(_ZH_Q, target_language=LANG_EN)
    assert result.text is None
    assert result.reason == reason


def test_translator_keeps_first_line_only_and_strips_quotes():
    provider = TranslatingProvider({_ZH_Q: '"Agency Partnerships share"\nNote: translated.'})
    assert LLMQueryTranslator(provider).translate(_ZH_Q, target_language=LANG_EN).text == _EN_Q


def test_provider_error_is_not_cached():
    provider = TranslatingProvider({_ZH_Q: _EN_Q}, fail=True)
    translator = LLMQueryTranslator(provider)
    assert translator.translate(_ZH_Q, target_language=LANG_EN).reason == "provider_error"
    provider.fail = False
    assert translator.translate(_ZH_Q, target_language=LANG_EN).text == _EN_Q


def test_mock_provider_does_not_translate():
    result = LLMQueryTranslator(MockProvider()).translate(_ZH_Q, target_language=LANG_EN)
    assert (result.text, result.reason) == (None, "unchanged")


# ---------------------------------------------------------------- retrieval: trigger / channels


def test_cross_language_query_triggers_translation_and_recovers_bm25(en_store, caplog):
    provider = TranslatingProvider({_ZH_Q: _EN_Q})
    base = NarrativeIndex(en_store, page_parent="off")
    assert base.retrieve(_ZH_Q, rerank=False) == []
    index = NarrativeIndex(
        en_store, page_parent="off", query_translator=LLMQueryTranslator(provider)
    )
    with caplog.at_level(logging.INFO):
        hits = index.retrieve(_ZH_Q, rerank=False)
        index.retrieve(_ZH_Q, rerank=False)
    assert hits and "Agency" in hits[0].chunk.text
    assert len(provider.calls) == 1  # 每个问题只翻译一次
    traces = _traces(caplog)
    assert [t.status for t in traces] == ["translated", "translated"]
    assert [t.cache_hit for t in traces] == [False, True]
    assert traces[0].query_language == LANG_ZH and traces[0].doc_language == LANG_EN
    assert all(t.n_extra_queries == 1 and t.n_fallback == 0 for t in traces)


def test_same_language_query_never_calls_provider(en_store, caplog):
    provider = TranslatingProvider({"distribution mix": "分销渠道"})
    index = NarrativeIndex(
        en_store, page_parent="off", query_translator=LLMQueryTranslator(provider)
    )
    plain = NarrativeIndex(en_store, page_parent="off")
    with caplog.at_level(logging.INFO):
        got = index.retrieve("distribution mix", rerank=False)
    assert got == plain.retrieve("distribution mix", rerank=False)
    assert provider.calls == []
    (trace,) = _traces(caplog)
    assert (trace.status, trace.reason, trace.n_extra_queries) == ("skipped", "same_language", 0)


def test_chinese_question_on_chinese_corpus_is_not_translated(tmp_path):
    from ragspine.retrieval.chunking.chunk_store import ChunkStore

    store = ChunkStore(tmp_path / "zh.db")
    store.init_schema()
    store.replace_doc_chunks(
        "zh",
        [
            _chunk(0, "中国内地 2024 年新业务价值增长 12%。"),
            _chunk(1, "香港分销渠道以代理人为主。"),
        ],
    )
    provider = TranslatingProvider({"香港 分销渠道": "Hong Kong distribution"})
    index = NarrativeIndex(store, page_parent="off", query_translator=LLMQueryTranslator(provider))
    assert index.retrieve("香港 分销渠道", rerank=False)
    assert provider.calls == []
    store.close()


def test_provider_unavailable_degrades_with_counted_trace(en_store, caplog):
    index = NarrativeIndex(
        en_store,
        page_parent="off",
        query_translator=LLMQueryTranslator(TranslatingProvider(fail=True)),
    )
    plain = NarrativeIndex(en_store, page_parent="off")
    with caplog.at_level(logging.INFO):
        got = index.retrieve(_ZH_Q, rerank=False)
    assert got == plain.retrieve(_ZH_Q, rerank=False)
    (trace,) = _traces(caplog)
    assert (trace.status, trace.reason, trace.n_fallback, trace.n_extra_queries) == (
        "fallback",
        "provider_error",
        1,
        0,
    )


def test_no_provider_degrades_with_counted_trace(en_store, caplog):
    index = NarrativeIndex(en_store, page_parent="off", query_translator=LLMQueryTranslator(None))
    with caplog.at_level(logging.INFO):
        assert index.retrieve(_ZH_Q, rerank=False) == []
    (trace,) = _traces(caplog)
    assert (trace.status, trace.reason, trace.n_fallback) == ("fallback", "no_provider", 1)


def test_trace_never_carries_question_or_translation(en_store, caplog):
    provider = TranslatingProvider({_ZH_Q: _EN_Q})
    index = NarrativeIndex(
        en_store, page_parent="page+child", query_translator=LLMQueryTranslator(provider)
    )
    with caplog.at_level(logging.INFO):
        index.retrieve(_ZH_Q, rerank=False)
    for record in caplog.records:
        for value in record.__dict__.values():
            if isinstance(value, str):
                assert _ZH_Q not in value and _EN_Q not in value


class _RecordingEmbedding:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_texts(self, texts):
        return [[float(len(t) % 7), 1.0] for t in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return [1.0, 1.0]


def test_translation_is_lexical_only_by_default():
    chunks = [_chunk(0, "Agency share 72%"), _chunk(1, "Singapore VONB")]
    emb = _RecordingEmbedding()
    retriever = HybridRetriever(chunks, embedding_backend=emb)
    lexical = retriever.search(_ZH_Q, extra_queries=[_EN_Q])
    assert emb.queries == [_ZH_Q]
    assert any(r.bm25_score > 0 for r in lexical)
    emb.queries.clear()
    retriever.search(_ZH_Q, extra_queries=[_EN_Q], extra_vector=True)
    assert emb.queries == [_ZH_Q, _EN_Q]


def test_empty_extra_queries_is_byte_identical():
    chunks = [_chunk(0, "Agency share 72%"), _chunk(1, "Singapore VONB share")]
    retriever = HybridRetriever(chunks)
    assert retriever.search("share") == retriever.search("share", extra_queries=())


def test_page_child_whole_page_bm25_also_uses_translation(en_store):
    provider = TranslatingProvider({_ZH_Q: _EN_Q})
    index = NarrativeIndex(
        en_store, page_parent="page+child", query_translator=LLMQueryTranslator(provider)
    )
    hits = index.retrieve(_ZH_Q, rerank=False)
    assert hits and hits[0].chunk.source_locator.startswith("deck.md@page=1")


# ---------------------------------------------------------------- wiring + generation isolation


def test_build_narrative_retriever_auto_uses_translation_provider(en_store, tmp_path):
    provider = TranslatingProvider({_ZH_Q: _EN_Q})
    retriever, store = build_narrative_retriever(
        tmp_path / "chunks.db", query_translation="auto", translation_provider=provider
    )
    try:
        assert retriever.retrieve(_ZH_Q)
    finally:
        store.close()
    assert len(provider.calls) == 1


def test_build_narrative_retriever_auto_falls_back_to_provider(en_store, tmp_path):
    provider = TranslatingProvider({_ZH_Q: _EN_Q})
    retriever, store = build_narrative_retriever(
        tmp_path / "chunks.db", provider=provider, reranker=_KeepJudge(), query_translation="auto"
    )
    try:
        assert retriever.retrieve(_ZH_Q)
    finally:
        store.close()
    assert len(provider.calls) == 1


class _KeepJudge:
    def judge(self, query, candidates):
        return list(range(len(candidates)))


class _AnswerProvider(TranslatingProvider):
    """翻译请求按表翻译；其余（生成）请求记下 messages 并回一句带出处的答案。"""

    def chat(self, messages, *, tools=None):
        if (
            messages
            and messages[0].get("role") == "system"
            and "search query" in str(messages[0].get("content"))
        ):
            return super().chat(messages, tools=tools)
        self.generation_calls.append(messages)
        return _completion_text("Agency 72%.")

    def __init__(self, table):
        super().__init__(table)
        self.generation_calls: list[list[dict]] = []


def test_translation_only_reaches_retrieval_never_the_generation_prompt(en_store, tmp_path):
    from ragspine.storage.fact_store import SqliteFactStore

    provider = _AnswerProvider({"分销渠道 占比是多少": _EN_Q})
    index = NarrativeIndex(
        en_store, page_parent="page+child", query_translator=LLMQueryTranslator(provider)
    )
    facts = SqliteFactStore(tmp_path / "facts.db")
    facts.init_schema()
    try:
        answer_question(
            "分销渠道 占比是多少",
            facts,
            provider,
            narrative_retriever=NarrativeIndexRetriever(index),
        )
    finally:
        facts.close()
    assert len(provider.calls) == 1  # 翻译调用一次
    assert provider.generation_calls, "应走到叙事生成"
    prompt = "\n".join(str(m.get("content")) for msgs in provider.generation_calls for m in msgs)
    assert "分销渠道 占比是多少" in prompt
    assert _EN_Q not in prompt


def test_build_narrative_retriever_auto_sends_translation_to_both_channels(en_store, tmp_path):
    retriever, store = build_narrative_retriever(
        tmp_path / "chunks.db",
        query_translation="auto",
        translation_provider=TranslatingProvider({_ZH_Q: _EN_Q}),
    )
    try:
        assert retriever.index.translate_vector is True
        assert isinstance(retriever.index.query_translator, LLMQueryTranslator)
    finally:
        store.close()


def test_build_narrative_retriever_defaults_to_auto(en_store, tmp_path):
    provider = TranslatingProvider({_ZH_Q: _EN_Q})
    retriever, store = build_narrative_retriever(
        tmp_path / "chunks.db", translation_provider=provider
    )
    try:
        assert retriever.retrieve(_ZH_Q)
    finally:
        store.close()
    assert len(provider.calls) == 1
