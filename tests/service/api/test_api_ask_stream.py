"""服务层 /v1/ask/stream SSE 行为测试。

镜像 test_api_ask.py 的 fixtures/helpers：核心不变量是 anti-fabrication 守卫在
SSE 流打开之前已跑完（not_found→拒答改写已生效），生成器只回放已守卫的 answer，
不触达 provider/store。逐条断言 stream 拼接 == /v1/ask 的 guarded answer。
"""

import json
import os

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider, ProviderError
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache, FAQItem
from ragspine.service.tasks.task_queue import FakeQueue
from ragspine.storage.fact_store import Fact, SqliteFactStore

REF_DATE = "2026-06-12"


# ---------------------------------------------------------------------------
# fixtures / helpers（镜像 test_api_ask.py）
# ---------------------------------------------------------------------------
@pytest.fixture
def seeded_db_path(tmp_path):
    db_path = tmp_path / "fact_metric.db"
    fs = SqliteFactStore(db_path)
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

    def chat(self, messages, *, tools=None):
        self.calls += 1
        if self.raise_provider_error:
            raise ProviderError("gateway down")
        raise AssertionError("SpyProvider.chat should not be called")


def parse_sse(body: str) -> list[dict]:
    """把 SSE 响应体按 `\\n\\n` 切帧，剥 `data: ` 前缀，逐条 json.loads。"""
    events = []
    for frame in body.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        assert frame.startswith("data: "), frame
        events.append(json.loads(frame[len("data: "):]))
    return events


def _deltas(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


def _done(events: list[dict]) -> dict:
    dones = [e for e in events if e["type"] == "done"]
    assert len(dones) == 1
    return dones[0]


# ---------------------------------------------------------------------------
# STREAM == GUARDED ANSWER
# ---------------------------------------------------------------------------
def test_stream_equals_guarded_answer(base_config):
    client = make_client(base_config)
    q = {"question": "香港去年REVENUE多少"}

    ask = client.post("/v1/ask", json=q).json()

    resp = client.post("/v1/ask/stream", json=q)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)

    assert events[0]["type"] == "start"
    assert events[0]["request_id"]
    assert _deltas(events) == ask["answer"]
    assert "1702" in _deltas(events)

    done = _done(events)
    assert done["route"] == ask["route"] == "structured"
    assert done["answer_kind"] == ask["answer_kind"]
    got_docs = {s.get("doc") for s in done["sources"]}
    ask_docs = {s.get("doc") for s in ask["sources"]}
    assert got_docs == ask_docs
    assert done["tool_status_summary"] == ask["tool_status_summary"]
    assert done["cache"]["hit"] is False


# ---------------------------------------------------------------------------
# ANTI-FABRICATION ON THE STREAM
# ---------------------------------------------------------------------------
def test_stream_not_found_is_refusal_and_never_fabricates(base_config):
    client = make_client(base_config)
    resp = client.post("/v1/ask/stream", json={"question": "中国去年ROE多少"})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    concatenated = _deltas(events)

    assert "查不到" in concatenated
    assert "1702" not in concatenated
    done = _done(events)
    assert done["answer_kind"] == "refusal"


# ---------------------------------------------------------------------------
# GUARD PRECEDES STREAM — every delta is a substring of the guarded refusal
# ---------------------------------------------------------------------------
def test_stream_guard_precedes_stream(base_config):
    client = make_client(base_config)
    resp = client.post("/v1/ask/stream", json={"question": "中国去年ROE多少"})
    events = parse_sse(resp.text)
    refusal = _deltas(events)
    done = _done(events)
    assert done["answer_kind"] == "refusal"
    for e in events:
        if e["type"] == "delta":
            assert e["text"] in refusal


# ---------------------------------------------------------------------------
# FAQ HIT — provider NOT called, cached answer streamed
# ---------------------------------------------------------------------------
def test_stream_faq_hit(base_config):
    spy = SpyProvider()
    faq = FAQCache([
        FAQItem(
            id="f1", question="RAGSpine 是什么",
            answer="RAGSpine 是高管经营洞察助手。",
            source="faq/handbook.md#what-is", version=3,
        ),
    ])
    client = make_client(base_config, provider=spy, faq_cache=faq)
    resp = client.post("/v1/ask/stream", json={"question": "RAGSpine 是什么"})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert _deltas(events) == "RAGSpine 是高管经营洞察助手。"
    done = _done(events)
    assert done["cache"]["hit"] is True
    assert done["route"] == "faq"
    assert done["answer_kind"] == "normal"
    assert spy.calls == 0


# ---------------------------------------------------------------------------
# PROVIDER ERROR — degrade, not 500
# ---------------------------------------------------------------------------
def test_stream_provider_error_degrades_not_500(base_config):
    provider = SpyProvider(raise_provider_error=True)
    client = make_client(base_config, provider=provider)
    resp = client.post("/v1/ask/stream", json={"question": "香港去年REVENUE多少"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    assert provider.calls >= 1
    concatenated = _deltas(events)
    assert "1702" not in concatenated
    assert "Traceback" not in concatenated
    assert concatenated
