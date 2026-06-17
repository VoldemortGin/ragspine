"""服务层 /v1/ask HTTP 行为测试（薄适配层，只验证外部行为）。

覆盖 PRD Testing Decisions：ask 等价性、澄清、防编造、叙事降级、provider error、
FAQ hit/miss/exclusion、敏感日志。注入临时 FactStore + MockProvider/spy provider +
FakeQueue + FAQCache，不依赖 Redis / 真实 LLM。
"""

import logging
import os

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.agent import answer_question
from ragspine.agent.llm_provider import MockProvider, ProviderError
from ragspine.common.observability import TRACE_LOGGER_NAME
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache, FAQItem
from ragspine.service.tasks.task_queue import FakeQueue
from ragspine.storage.fact_store import Fact, FactStore

REF_DATE = "2026-06-12"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def seeded_db_path(tmp_path):
    db_path = tmp_path / "fact_metric.db"
    fs = FactStore(db_path)
    fs.init_schema()
    fs.upsert_facts([
        Fact(
            metric_code="REVENUE", entity="ACME_HK", geography="HK", channel="TOTAL",
            period_type="FY", period="2025", value=1702.0, unit="USD_M",
            source_doc_id="ACME_FY2025_Results.pptx",
            source_locator="slide=5,table=1,row=2,col=3",
        ),
    ])
    fs.close()
    return db_path


@pytest.fixture
def base_config(seeded_db_path):
    return ServiceConfig(db_path=str(seeded_db_path), reference_date=REF_DATE)


def make_client(config, *, provider=None, queue=None, faq_cache=None):
    provider = provider or MockProvider(reference_date=config.reference_date_obj())
    queue = queue or FakeQueue()
    faq_cache = faq_cache or FAQCache.empty()
    app = create_app(config, provider=provider, queue=queue, faq_cache=faq_cache)
    return TestClient(app)


class SpyProvider:
    """provider 间谍：被调用即记录；可选抛 ProviderError。"""

    def __init__(self, *, raise_provider_error=False):
        self.calls = 0
        self.raise_provider_error = raise_provider_error

    def create_message(self, *, system, messages, tools):
        self.calls += 1
        if self.raise_provider_error:
            raise ProviderError("gateway down")
        raise AssertionError("SpyProvider.create_message should not be called")


# ---------------------------------------------------------------------------
# ASK EQUIVALENCE
# ---------------------------------------------------------------------------
def test_ask_equivalence_with_workflow(base_config, seeded_db_path):
    client = make_client(base_config)
    resp = client.post("/v1/ask", json={"question": "香港去年REVENUE多少"})
    assert resp.status_code == 200
    body = resp.json()

    # 直接调用 workflow 取参照
    store = FactStore(seeded_db_path)
    store.init_schema()
    ref = base_config.reference_date_obj()
    expected = answer_question(
        "香港去年REVENUE多少", store,
        MockProvider(reference_date=ref), reference_date=ref,
    )
    store.close()

    assert body["answer"] == expected.answer
    assert body["route"] == expected.route
    assert body["route"] == "structured"
    assert "1702" in body["answer"]
    expected_docs = {s.get("doc") for s in expected.sources}
    got_docs = {s.get("doc") for s in body["sources"]}
    assert got_docs == expected_docs
    assert "ACME_FY2025_Results.pptx" in got_docs
    assert body["answer_kind"] == "normal"
    assert body["cache"]["hit"] is False
    assert body["tool_status_summary"]["found"] == 1
    assert "request_id" in body and body["request_id"]


# ---------------------------------------------------------------------------
# CLARIFICATION — missing metric -> clarification, provider NOT called
# ---------------------------------------------------------------------------
def test_ask_clarification_does_not_call_provider(base_config):
    spy = SpyProvider()
    client = make_client(base_config, provider=spy)
    resp = client.post("/v1/ask", json={"question": "香港去年多少"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer_kind"] == "clarification"
    assert body["clarification"] is not None
    assert body["clarification"]["mode"] == "ask_first"
    assert spy.calls == 0


# ---------------------------------------------------------------------------
# ANTI-FABRICATION — not_found -> deterministic refusal
# ---------------------------------------------------------------------------
def test_ask_not_found_is_refusal_and_never_fabricates(base_config):
    client = make_client(base_config)
    resp = client.post("/v1/ask", json={"question": "中国去年ROE多少"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer_kind"] == "refusal"
    assert "查不到" in body["answer"]
    assert "1702" not in body["answer"]
    assert body["tool_status_summary"]["not_found"] >= 1
    assert body["tool_status_summary"]["found"] == 0


# ---------------------------------------------------------------------------
# NARRATIVE DEGRADATION — no chunk_db_path -> honest degraded, no crash
# ---------------------------------------------------------------------------
def test_ask_narrative_degrades_without_retriever(base_config):
    client = make_client(base_config)
    resp = client.post("/v1/ask", json={"question": "为什么香港REVENUE会变化"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] in ("narrative", "composite")
    assert body["answer"]  # 有坦白降级文案，不崩


# ---------------------------------------------------------------------------
# PROVIDER ERROR — ProviderError -> HTTP 200 honest degraded (NOT 500)
# ---------------------------------------------------------------------------
def test_ask_provider_error_degrades_not_500(base_config):
    provider = SpyProvider(raise_provider_error=True)
    client = make_client(base_config, provider=provider)
    resp = client.post("/v1/ask", json={"question": "香港去年REVENUE多少"})
    assert resp.status_code == 200
    body = resp.json()
    assert provider.calls >= 1
    # 诚实降级文案，无 traceback / 无编造数字
    assert "1702" not in body["answer"]
    assert "Traceback" not in body["answer"]
    assert body["answer"]


# ---------------------------------------------------------------------------
# FAQ HIT — cache short-circuit, provider spy NOT called
# ---------------------------------------------------------------------------
def test_ask_faq_hit(base_config):
    spy = SpyProvider()
    faq = FAQCache([
        FAQItem(
            id="f1", question="RAGSpine 是什么",
            answer="RAGSpine 是高管经营洞察助手。",
            source="faq/handbook.md#what-is", version=3,
            aliases=("什么是 RAGSpine",),
        ),
    ])
    client = make_client(base_config, provider=spy, faq_cache=faq)
    resp = client.post("/v1/ask", json={"question": "RAGSpine 是什么"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cache"]["hit"] is True
    assert body["cache"]["type"] == "faq"
    assert body["cache"]["faq_id"] == "f1"
    assert body["cache"]["version"] == 3
    assert body["cache"]["source"] == "faq/handbook.md#what-is"
    assert body["route"] == "faq"
    assert body["answer_kind"] == "normal"
    assert body["answer"] == "RAGSpine 是高管经营洞察助手。"
    assert any(s.get("doc") == "faq/handbook.md#what-is" for s in body["sources"])
    assert spy.calls == 0


def test_ask_faq_hit_via_alias(base_config):
    spy = SpyProvider()
    faq = FAQCache([
        FAQItem(
            id="f1", question="RAGSpine 是什么",
            answer="RAGSpine 是高管经营洞察助手。",
            source="faq/handbook.md", version=1,
            aliases=("什么是 RAGSpine",),
        ),
    ])
    client = make_client(base_config, provider=spy, faq_cache=faq)
    resp = client.post("/v1/ask", json={"question": "什么是 RAGSpine"})
    body = resp.json()
    assert body["cache"]["hit"] is True
    assert spy.calls == 0


# ---------------------------------------------------------------------------
# FAQ MISS — equivalent to normal ask
# ---------------------------------------------------------------------------
def test_ask_faq_miss_equivalent_to_normal(base_config):
    empty = FAQCache.empty()
    client_empty = make_client(base_config, faq_cache=empty)
    resp_empty = client_empty.post("/v1/ask", json={"question": "香港去年REVENUE多少"})

    # 有 FAQ 但不匹配此问 -> 仍走正常 workflow
    faq = FAQCache([FAQItem(id="z", question="完全无关的问题", answer="x")])
    client_faq = make_client(base_config, faq_cache=faq)
    resp_faq = client_faq.post("/v1/ask", json={"question": "香港去年REVENUE多少"})

    a, b = resp_empty.json(), resp_faq.json()
    assert a["answer"] == b["answer"]
    assert a["route"] == b["route"]
    assert a["cache"]["hit"] is False
    assert b["cache"]["hit"] is False


# ---------------------------------------------------------------------------
# FAQ EXCLUSION via HTTP — structured numeric question never short-circuits
# ---------------------------------------------------------------------------
def test_ask_faq_does_not_shortcircuit_structured(base_config):
    # 即便 FAQ 里有一条文本恰好等于该结构化数字问题，也不得短路
    faq = FAQCache([
        FAQItem(id="bad", question="香港去年REVENUE多少", answer="伪造的缓存答案"),
    ])
    client = make_client(base_config, faq_cache=faq)
    resp = client.post("/v1/ask", json={"question": "香港去年REVENUE多少"})
    body = resp.json()
    assert body["cache"]["hit"] is False
    assert body["answer"] != "伪造的缓存答案"
    assert "1702" in body["answer"]
    assert body["route"] == "structured"


# ---------------------------------------------------------------------------
# SENSITIVE LOGGING — trace must not leak question/answer/source/fact values
# ---------------------------------------------------------------------------
def test_ask_trace_does_not_leak_sensitive(base_config, caplog):
    client = make_client(base_config)
    with caplog.at_level(logging.INFO, logger=TRACE_LOGGER_NAME):
        client.post("/v1/ask", json={"question": "香港去年REVENUE多少"})

    records = [r for r in caplog.records if r.name == TRACE_LOGGER_NAME]
    assert records  # 确有 trace 产生
    forbidden = ["香港去年REVENUE多少", "1702", "ACME_FY2025_Results.pptx",
                 "slide=5,table=1,row=2,col=3"]
    for rec in records:
        # message 固定为 "trace"
        assert rec.getMessage() == "trace"
        blob = " ".join(str(v) for v in rec.__dict__.values())
        for secret in forbidden:
            assert secret not in blob, f"trace leaked {secret!r}"


def test_ask_faq_hit_trace_does_not_leak(base_config, caplog):
    faq = FAQCache([
        FAQItem(id="f1", question="RAGSpine 是什么",
                answer="它是高管经营洞察助手机密内容XYZ。", source="faq/x.md"),
    ])
    client = make_client(base_config, faq_cache=faq)
    with caplog.at_level(logging.INFO, logger=TRACE_LOGGER_NAME):
        client.post("/v1/ask", json={"question": "RAGSpine 是什么"})
    records = [r for r in caplog.records if r.name == TRACE_LOGGER_NAME]
    assert records
    for rec in records:
        blob = " ".join(str(v) for v in rec.__dict__.values())
        assert "机密内容XYZ" not in blob
        assert "RAGSpine 是什么" not in blob
        # 但 faq_id 可观测
    assert any("f1" in " ".join(str(v) for v in r.__dict__.values()) for r in records)


# ---------------------------------------------------------------------------
# request-level reference_date override
# ---------------------------------------------------------------------------
def test_ask_request_reference_date_override(seeded_db_path):
    # config 无 reference_date，请求体显式给出 -> 仍能解析"去年"=FY2025
    config = ServiceConfig(db_path=str(seeded_db_path))
    client = make_client(config)
    resp = client.post(
        "/v1/ask",
        json={"question": "香港去年REVENUE多少", "reference_date": REF_DATE},
    )
    body = resp.json()
    assert "1702" in body["answer"]
