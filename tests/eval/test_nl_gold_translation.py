"""nl-gold 评测产物里的查询译文（``translated_query``）。

被测规格：
- 翻译器 ``translations()`` 暴露已缓存的可用译文 {问题: 译文}；失败结果（无译文）不在其中。
- ``build_narrative_retriever(query_translator=...)`` 可注入翻译器，检索照常用它。
- ``run_route(translation_of=...)`` 把本次检索用到的译文记进 ``CaseRun.translated_query``；没翻译 / 没检索为 None。
- report.json 每条记录带 ``translated_query``，report.md 失败清单显示译文；旧报告重判时缺该字段记 None。
- 隐私：译文只进评测产物，observability trace 里没有原文也没有译文。
"""

import json
import logging
import os
from datetime import date
from pathlib import Path

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider, _completion_text
from ragspine.common.observability.trace import TRACE_LOGGER_NAME
from ragspine.eval.nl_gold_ragspine import (
    ROUTE_FORCED_NARRATIVE,
    CaseRun,
    ClaimAnchor,
    GoldCase,
    rejudge_report,
    run_route,
    write_report,
)
from ragspine.retrieval.link.narrative_link import build_narrative_retriever
from ragspine.retrieval.translation import LANG_EN, LLMQueryTranslator
from ragspine.session import RAGSpine
from ragspine.storage.fact_store import SqliteFactStore
from tests.eval.test_nl_gold_ragspine import _DECK

ZH_Q = "代理人渠道的新业务价值占比是多少"
EN_TRANSLATION = "Agency share of VONB"
EN_Q = "What was the record ROE?"


class _TranslatingProvider:
    """按 user 消息查表翻译（查不到返回空串）；记录调用次数。"""

    def __init__(self, table: dict[str, str]) -> None:
        self.table = table
        self.calls = 0

    def chat(self, messages, *, tools=None):  # noqa: ANN001, ANN201
        self.calls += 1
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        return _completion_text(self.table.get(user, ""))


def _cases() -> tuple[GoldCase, ...]:
    return (
        GoldCase(
            "p-mix",
            "positive",
            (("zh", ZH_Q),),
            required_claims=((ClaimAnchor(kind="quote", page_index=1, quote="99%"),),),
        ),
        GoldCase(
            "p-roe",
            "positive",
            (("en", EN_Q),),
            required_claims=((ClaimAnchor(kind="quote", page_index=2, quote="17.5%"),),),
        ),
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    deck = tmp_path / "deck.md"
    deck.write_text(_DECK, encoding="utf-8")
    RAGSpine.local(ws).ingest(deck)
    return ws / "knowledge.db"


def test_translations_expose_usable_cached_translations_only() -> None:
    translator = LLMQueryTranslator(_TranslatingProvider({ZH_Q: EN_TRANSLATION}))
    assert translator.translations() == {}
    translator.translate(ZH_Q, target_language=LANG_EN)
    translator.translate("另一个问题", target_language=LANG_EN)  # 空输出 → 无可用译文
    assert translator.translations() == {ZH_Q: EN_TRANSLATION}


def test_build_narrative_retriever_uses_an_injected_translator(db: Path) -> None:
    provider = _TranslatingProvider({ZH_Q: EN_TRANSLATION})
    translator = LLMQueryTranslator(provider)
    retriever, store = build_narrative_retriever(db, query_translator=translator)
    try:
        assert retriever.retrieve(ZH_Q)
    finally:
        store.close()
    assert provider.calls == 1
    assert translator.translations() == {ZH_Q: EN_TRANSLATION}


def _run(db: Path, *, translation_of=None) -> list[CaseRun]:  # noqa: ANN001
    translator = LLMQueryTranslator(_TranslatingProvider({ZH_Q: EN_TRANSLATION}))
    retriever, chunk_store = build_narrative_retriever(db, query_translator=translator)
    fact_store = SqliteFactStore(db)
    fact_store.init_schema()
    try:
        return run_route(
            _cases(),
            ROUTE_FORCED_NARRATIVE,
            store=fact_store,
            retriever=retriever,
            provider=MockProvider(),
            reference_date=date(2026, 9, 23),
            translation_of=(
                (lambda q: translator.translations().get(q)) if translation_of else None
            ),
        )
    finally:
        fact_store.close()
        chunk_store.close()


def test_run_route_records_the_translated_query(
    db: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER_NAME)
    runs = {r.case_id: r for r in _run(db, translation_of=True)}
    assert runs["p-mix"].translated_query == EN_TRANSLATION
    assert runs["p-roe"].translated_query is None  # 与文档同语言，不翻译

    out = write_report(
        tmp_path / "report",
        meta={"label": "t"},
        cases=_cases(),
        runs={ROUTE_FORCED_NARRATIVE: list(runs.values())},
    )
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    records = {r["case_id"]: r for r in report["cases"][ROUTE_FORCED_NARRATIVE]}
    assert records["p-mix"]["translated_query"] == EN_TRANSLATION
    assert records["p-roe"]["translated_query"] is None
    raw = json.loads(
        (out / "cases" / ROUTE_FORCED_NARRATIVE / "p-mix-zh.json").read_text(encoding="utf-8")
    )
    assert raw["translated_query"] == EN_TRANSLATION
    md = (out / "report.md").read_text(encoding="utf-8")
    assert not runs["p-mix"].judgement.passed
    assert f"translated query: {EN_TRANSLATION}" in md

    # 隐私：原文与译文只进评测产物，绝不进 observability trace。
    ops = [getattr(record, "op", None) for record in caplog.records]
    assert "narrative.query_translation" in ops
    traced = "\n".join(str(vars(record)) for record in caplog.records)
    assert ZH_Q not in traced and EN_TRANSLATION not in traced


def test_translated_query_defaults_to_none(db: Path) -> None:
    assert all(r.translated_query is None for r in _run(db))


def test_rejudge_of_an_old_report_without_translated_query(db: Path, tmp_path: Path) -> None:
    runs = _run(db, translation_of=True)
    out = write_report(tmp_path / "old", meta={"label": "old"}, cases=_cases(), runs={"B": runs})
    report_path = out / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    for record in report["cases"]["B"]:
        del record["translated_query"]
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    rejudged = rejudge_report(report_path, _cases())["B"]
    assert [r.translated_query for r in rejudged] == [None, None]
