"""/v1/topology 拓扑导出接口测试：三种 scope + 非法 scope 错误形状。"""

import os

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import FakeQueue


@pytest.fixture
def client(tmp_path):
    config = ServiceConfig(db_path=str(tmp_path / "fact.db"))
    app = create_app(
        config, provider=MockProvider(), queue=FakeQueue(), faq_cache=FAQCache.empty()
    )
    return TestClient(app)


def _assert_graph_shape(body):
    assert body["request_id"]
    assert body["title"]
    assert body["nodes"] and body["edges"]
    for node in body["nodes"]:
        assert set(node) == {"id", "label", "kind", "domain", "symbol"}
        assert node["id"] and node["label"] and node["kind"]
    for edge in body["edges"]:
        assert set(edge) == {"src", "dst", "label", "kind"}
        assert edge["src"] and edge["dst"] and edge["kind"]


def test_topology_default_scope_is_agent(client):
    resp = client.get("/v1/topology")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Agent request flow"
    _assert_graph_shape(body)


def test_topology_agent_scope(client):
    resp = client.get("/v1/topology", params={"scope": "agent"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Agent request flow"
    _assert_graph_shape(body)
    node_ids = {n["id"] for n in body["nodes"]}
    assert {"parse", "tool_loop", "retrieve", "narrative_answer"} <= node_ids
    route_labels = {
        edge["label"]
        for edge in body["edges"]
        if edge["src"] == "route"
    }
    assert {"route=structured", "route=narrative", "route=composite"} <= route_labels


def test_topology_retriever_scope(client):
    resp = client.get("/v1/topology", params={"scope": "retriever"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "HybridRetriever sub-pipeline"
    _assert_graph_shape(body)
    node_ids = {n["id"] for n in body["nodes"]}
    assert {"prefilter", "bm25", "rrf", "top_k"} <= node_ids
    assert "vector" not in node_ids


def test_topology_service_scope(client):
    resp = client.get("/v1/topology", params={"scope": "service"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Service topology"
    _assert_graph_shape(body)
    # faq_cache 与 queue 已装配 -> 对应节点如实出现
    node_ids = {n["id"] for n in body["nodes"]}
    assert {"faq", "agent", "queue"} <= node_ids


def test_topology_invalid_scope_400(client):
    resp = client.get("/v1/topology", params={"scope": "bogus"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["type"] == "InvalidScope"
    assert "agent, retriever, service" in err["message"]
    assert err["request_id"]
