"""工作流 catalog/scaffold HTTP 边界：只读、离线且不接受执行能力注入。"""

import json
import os
import socket
from collections.abc import Sequence

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider  # noqa: E402
from ragspine.service.api.app import create_app  # noqa: E402
from ragspine.service.config import ServiceConfig  # noqa: E402
from ragspine.service.faq.faq_cache import FAQCache  # noqa: E402
from ragspine.service.tasks.task_queue import FakeQueue  # noqa: E402
from ragspine.workflows.matching import EmbeddingTemplateMatcher  # noqa: E402
from ragspine.workflows.model import TemplateMatch, WorkflowTemplate  # noqa: E402


class _SemanticMatcher:
    name = "semantic"
    reuse_threshold = 0.82
    reuse_margin = 0.05

    def __init__(self) -> None:
        self.calls = 0

    def rank(
        self, query: str, templates: Sequence[WorkflowTemplate]
    ) -> tuple[TemplateMatch, ...]:
        del query
        self.calls += 1
        return (TemplateMatch(templates[0], confidence=1.0, matcher=self.name),)


class _FailingEmbeddingBackend:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise RuntimeError("sk-super-secret")


@pytest.fixture
def client(tmp_path) -> TestClient:
    config = ServiceConfig(db_path=str(tmp_path / "fact.db"))
    app = create_app(
        config,
        provider=MockProvider(),
        queue=FakeQueue(),
        faq_cache=FAQCache.empty(),
    )
    return TestClient(app)


def _templates(client: TestClient) -> list[dict[str, object]]:
    response = client.get("/v1/workflow-templates")
    assert response.status_code == 200
    body = response.json()
    assert body["request_id"]
    assert body["templates"]
    return body["templates"]


def test_workflow_template_list_is_metadata_only(client: TestClient) -> None:
    templates = _templates(client)

    for template in templates:
        assert {
            "id",
            "name",
            "description",
            "compatibility",
            "requirements",
            "source",
        } <= set(template)
        assert "yaml" not in template
        assert "dify_yaml" not in template


def test_workflow_template_detail_includes_yaml(client: TestClient) -> None:
    template_id = str(_templates(client)[0]["id"])

    response = client.get(f"/v1/workflow-templates/{template_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"]
    assert body["id"] == template_id
    assert isinstance(body["workflow"], dict)
    assert body["yaml"]
    assert body["compatibility"]
    assert isinstance(body["requirements"], list)
    assert "source" in body


def test_workflow_template_detail_missing_is_404(client: TestClient) -> None:
    response = client.get("/v1/workflow-templates/not-a-template")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "workflow.template_not_found"


def test_workflow_scaffold_generates_offline_when_reuse_disabled(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("workflow scaffold must stay offline")

    def reject_execute(*args: object, **kwargs: object) -> None:
        raise AssertionError("workflow scaffold must not execute")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    from ragspine.service.dify import runner

    monkeypatch.setattr(runner, "run_workflow_isolated", reject_execute)

    response = client.post(
        "/v1/workflow-scaffold",
        json={"description": "Extract forms from papers with RAG", "reuse": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["request_id"]
    assert body["origin"] == "generated"
    assert isinstance(body["workflow"], dict)
    assert body["yaml"]
    assert body["compatibility"]
    assert isinstance(body["requirements"], list)
    assert "source" in body
    assert body["source"] is None
    assert 0.0 <= body["confidence"] <= 1.0
    assert body["matcher"]
    assert isinstance(body["warnings"], list)


def test_workflow_scaffold_reuses_explicit_catalog_template(client: TestClient) -> None:
    template = _templates(client)[0]
    template_id = str(template["id"])

    response = client.post(
        "/v1/workflow-scaffold",
        json={
            "description": str(template["description"]),
            "template_id": template_id,
            "reuse": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["origin"] == "template"
    assert body["template_id"] == template_id
    assert isinstance(body["workflow"], dict)
    assert body["yaml"]
    assert body["matcher"]


def test_workflow_scaffold_reuses_cached_injected_semantic_matcher(tmp_path) -> None:
    matcher = _SemanticMatcher()
    app = create_app(
        ServiceConfig(db_path=str(tmp_path / "fact.db")),
        provider=MockProvider(),
        queue=FakeQueue(),
        faq_cache=FAQCache.empty(),
        workflow_matcher=matcher,
    )
    semantic_client = TestClient(app)

    first = semantic_client.post(
        "/v1/workflow-scaffold", json={"description": "semantic paper request"}
    )
    second = semantic_client.post(
        "/v1/workflow-scaffold", json={"description": "another semantic request"}
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["matcher"] == second.json()["matcher"] == "semantic"
    assert first.json()["origin"] == second.json()["origin"] == "template"
    assert matcher.calls == 2
    assert app.state.workflow_matcher is matcher


def test_workflow_scaffold_safely_falls_back_when_semantic_backend_fails(tmp_path) -> None:
    matcher = EmbeddingTemplateMatcher(_FailingEmbeddingBackend(), name="semantic")
    app = create_app(
        ServiceConfig(db_path=str(tmp_path / "fact.db")),
        provider=MockProvider(),
        queue=FakeQueue(),
        faq_cache=FAQCache.empty(),
        workflow_matcher=matcher,
    )

    response = TestClient(app).post(
        "/v1/workflow-scaffold",
        json={"description": "A rag form understanding paper of CNN"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["matcher"] == "lexical"
    assert body["template_id"] == "rag-paper-qa"
    assert any("lexical" in warning.lower() for warning in body["warnings"])
    assert "sk-super-secret" not in response.text


def test_workflow_scaffold_missing_explicit_template_is_404(client: TestClient) -> None:
    response = client.post(
        "/v1/workflow-scaffold",
        json={
            "description": "Build a paper RAG workflow",
            "template_id": "not-a-template",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "workflow.template_not_found"


@pytest.mark.parametrize("description", ["", "x" * 4097])
def test_workflow_scaffold_rejects_description_outside_length_bounds(
    client: TestClient,
    description: str,
) -> None:
    response = client.post(
        "/v1/workflow-scaffold",
        json={"description": description},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("description", ["x", "x" * 4096])
def test_workflow_scaffold_accepts_description_length_boundaries(
    client: TestClient,
    description: str,
) -> None:
    response = client.post(
        "/v1/workflow-scaffold",
        json={"description": description, "reuse": False},
    )

    assert response.status_code == 200


@pytest.mark.parametrize("field", ["provider", "provider_expr", "api_key", "path", "url"])
def test_workflow_scaffold_forbids_client_capability_injection(
    client: TestClient,
    field: str,
) -> None:
    response = client.post(
        "/v1/workflow-scaffold",
        json={
            "description": "Build an offline paper RAG workflow",
            field: "sk-super-secret",
        },
    )

    assert response.status_code == 422
    assert "sk-super-secret" not in response.text


@pytest.mark.parametrize(
    ("content_type", "content"),
    [
        ("application/yaml", "description: Build a paper RAG workflow"),
        ("application/toml", 'description = "Build a paper RAG workflow"'),
    ],
)
def test_workflow_scaffold_only_accepts_json_envelope(
    client: TestClient,
    content_type: str,
    content: str,
) -> None:
    response = client.post(
        "/v1/workflow-scaffold",
        content=content,
        headers={"content-type": content_type},
    )

    assert response.status_code == 422


def test_workflow_responses_do_not_contain_secret_values(client: TestClient) -> None:
    list_response = client.get("/v1/workflow-templates")
    template_id = str(list_response.json()["templates"][0]["id"])
    detail_response = client.get(f"/v1/workflow-templates/{template_id}")
    scaffold_response = client.post(
        "/v1/workflow-scaffold",
        json={"description": "A research paper RAG workflow", "reuse": False},
    )

    combined = json.dumps(
        [list_response.json(), detail_response.json(), scaffold_response.json()],
        ensure_ascii=False,
    ).lower()
    assert "sk-super-secret" not in combined
    assert "-----begin private key-----" not in combined
    assert "bearer eyj" not in combined
