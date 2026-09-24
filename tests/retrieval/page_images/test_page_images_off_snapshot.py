"""页图开关关闭（默认 off）时，叙事问答送进 provider 的消息与引入页图之前逐字节一致。

冻结摘要取自引入页图之前的实现（HEAD 26fd3d7）：同一语料（页级父子共享语料）、同一组问题，
页级父子 off / dedup / page+child 三种装配下，``open_narrative_retriever`` + ``answer_question``
（强制叙事路由）送进 provider 的全部 messages、最终回答与来源的 JSON 摘要。默认 ServiceConfig 与
显式 ``page_images="off"`` 必须命中同一摘要。
"""

import hashlib
import json
import os
from dataclasses import replace
from datetime import date

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question
from ragspine.agent.intent import ROUTE_NARRATIVE, RuleIntentParser
from ragspine.agent.llm_provider import MockProvider
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from ragspine.service.config import ServiceConfig, open_narrative_retriever
from ragspine.storage.fact_store import SqliteFactStore

from ..page_parent.conftest import load_page_corpus

_QUESTIONS = ("Singapore VONB", "Hong Kong revenue", "Singapore growth outlook", "margins")
_REF = date(2026, 9, 1)

_FROZEN = "1fa01449a2d8e8f541a8fab21230d553d77c1bfaab9bc0ec3e9dd6136cb49aa9"


class _ForcedNarrative:
    def parse(self, question, *, reference_date=None):
        return replace(
            RuleIntentParser().parse(question, reference_date=reference_date), route=ROUTE_NARRATIVE
        )


class _RecordingProvider(MockProvider):
    def __init__(self) -> None:
        super().__init__(reference_date=_REF)
        self.seen: list[object] = []

    def chat(self, messages, *, tools=None):
        self.seen.append(messages)
        return super().chat(messages, tools=tools)


def _digest(tmp_path, **config_kwargs) -> str:
    db = tmp_path / "knowledge.db"
    store = ChunkStore(db)
    load_page_corpus(store)
    store.close()
    facts = SqliteFactStore(db)
    facts.init_schema()
    dumps: list[object] = []
    try:
        for mode in ("off", "dedup", "page+child"):
            config = ServiceConfig(
                db_path=str(db),
                chunk_db_path=str(db),
                embedding="none",
                page_parent=mode,
                **config_kwargs,
            )
            provider = _RecordingProvider()
            with open_narrative_retriever(config, provider) as retriever:
                for question in _QUESTIONS:
                    result = answer_question(
                        question,
                        facts,
                        provider,
                        reference_date=_REF,
                        narrative_retriever=retriever,
                        intent_parser=_ForcedNarrative(),
                    )
                    dumps.append([result.answer, result.sources])
            dumps.append(provider.seen)
    finally:
        facts.close()
    payload = json.dumps(dumps, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_default_config_messages_byte_identical(tmp_path):
    assert _digest(tmp_path) == _FROZEN


def test_explicit_off_messages_byte_identical(tmp_path):
    assert _digest(tmp_path, page_images="off") == _FROZEN
