"""Deterministic matching and safe workflow scaffolding contracts."""

from __future__ import annotations

import math
from typing import cast

import pytest

from ragspine.dify.api import compile_dify_yaml
from ragspine.workflows.catalog import load_builtin_catalog
from ragspine.workflows.errors import (
    WorkflowInputError,
    WorkflowMatcherError,
    WorkflowTemplateNotFoundError,
)
from ragspine.workflows.formats import parse_workflow
from ragspine.workflows.matching import (
    EmbeddingTemplateMatcher,
    LexicalTemplateMatcher,
    choose_reusable,
    make_template_matcher,
)
from ragspine.workflows.model import TemplateMatch
from ragspine.workflows.planner import MAX_DESCRIPTION_CHARS
from ragspine.workflows.scaffold import scaffold_workflow


class _StaticEmbeddingBackend:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        del texts
        return self.vectors


class _FirstTemplateBackend:
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.ambiguous = ambiguous

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = [[1.0, 0.0]]
        for index, _ in enumerate(texts[1:]):
            vectors.append(
                [1.0, 0.0] if index == 0 or (self.ambiguous and index == 1) else [0.0, 1.0]
            )
        return vectors


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("A rag form understanding paper of CNN", "rag-paper-qa"),
        ("把长报告整理成管理层摘要", "executive-summary"),
    ],
)
def test_lexical_matcher_has_deterministic_english_and_chinese_goldens(
    query: str, expected_id: str
) -> None:
    matches = LexicalTemplateMatcher().rank(query, load_builtin_catalog().runnable())

    assert matches[0].template.id == expected_id
    assert matches[0].confidence == 1.0
    assert matches[0].matcher == "lexical"


def test_embedding_matcher_uses_injected_cosine_backend() -> None:
    templates = load_builtin_catalog().runnable()[:2]
    matcher = EmbeddingTemplateMatcher(
        _StaticEmbeddingBackend([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]),
        name="test-semantic",
    )

    matches = matcher.rank("semantic request", templates)

    assert [match.template.id for match in matches] == [templates[0].id, templates[1].id]
    assert matches[0].confidence == pytest.approx(1.0)
    assert matches[1].confidence == pytest.approx(0.0)
    assert all(match.matcher == "test-semantic" for match in matches)


@pytest.mark.parametrize(
    "vectors",
    [
        [[1.0, 0.0]],
        [[1.0, 0.0], [1.0]],
        [[1.0, 0.0], [math.nan, 0.0]],
    ],
)
def test_embedding_matcher_rejects_invalid_backend_results(
    vectors: list[list[float]],
) -> None:
    template = load_builtin_catalog().runnable()[:1]
    matcher = EmbeddingTemplateMatcher(_StaticEmbeddingBackend(vectors))

    with pytest.raises(WorkflowMatcherError):
        matcher.rank("query", template)


def test_embedding_matcher_normalizes_backend_failure_without_reflecting_secret() -> None:
    class FailingBackend:
        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            del texts
            raise RuntimeError("sk-super-secret")

    matcher = EmbeddingTemplateMatcher(FailingBackend())

    with pytest.raises(WorkflowMatcherError) as exc_info:
        matcher.rank("query", load_builtin_catalog().runnable()[:1])

    assert "sk-super-secret" not in str(exc_info.value)


def test_workflow_matcher_factory_auto_falls_back_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_module = __import__(
        "ragspine.retrieval.vector.embedding_backends", fromlist=["make_embedding_backend"]
    )
    monkeypatch.setattr(embedding_module, "make_embedding_backend", lambda spec: None)

    assert isinstance(make_template_matcher("auto"), LexicalTemplateMatcher)
    assert isinstance(make_template_matcher("none"), LexicalTemplateMatcher)


def test_reuse_requires_both_threshold_and_unambiguous_margin() -> None:
    templates = load_builtin_catalog().runnable()[:2]
    leader = TemplateMatch(templates[0], confidence=0.90, matcher="test")
    close_runner_up = TemplateMatch(templates[1], confidence=0.87, matcher="test")
    low_leader = TemplateMatch(templates[0], confidence=0.70, matcher="test")

    assert choose_reusable([leader], threshold=0.82, margin=0.05) is leader
    assert choose_reusable([leader, close_runner_up], threshold=0.82, margin=0.05) is None
    assert choose_reusable([low_leader], threshold=0.82, margin=0.05) is None


@pytest.mark.parametrize(("threshold", "margin"), [(-0.1, 0.1), (0.5, 1.1)])
def test_reuse_rejects_invalid_policy_values(threshold: float, margin: float) -> None:
    with pytest.raises(ValueError):
        choose_reusable([], threshold=threshold, margin=margin)


def test_scaffold_reuses_exact_catalog_match() -> None:
    result = scaffold_workflow("A rag form understanding paper of CNN")

    assert result.origin == "template"
    assert result.template_id == "rag-paper-qa"
    assert result.confidence == 1.0
    assert result.matcher == "lexical"


def test_scaffold_reuses_injected_semantic_leader() -> None:
    matcher = EmbeddingTemplateMatcher(_FirstTemplateBackend(), name="fake-onnx")

    result = scaffold_workflow("semantically equivalent request", matcher=matcher)

    assert result.origin == "template"
    assert result.template_id == load_builtin_catalog().runnable()[0].id
    assert result.matcher == "fake-onnx"


def test_scaffold_generates_when_semantic_top_two_are_ambiguous() -> None:
    matcher = EmbeddingTemplateMatcher(_FirstTemplateBackend(ambiguous=True), name="fake-onnx")

    result = scaffold_workflow("ambiguous semantic request", matcher=matcher)

    assert result.origin == "generated"
    assert result.template_id is None
    assert result.matcher == "fake-onnx"


def test_unrelated_request_generates_safe_minimal_workflow() -> None:
    result = scaffold_workflow("quantum zeolite calibration telemetry")
    graph = cast(dict[str, object], result.workflow["workflow"])["graph"]
    nodes = cast(dict[str, object], graph)["nodes"]
    node_types = [
        cast(dict[str, object], cast(dict[str, object], node)["data"])["type"]
        for node in cast(list[object], nodes)
    ]

    assert result.origin == "generated"
    assert node_types == ["start", "llm", "end"]
    assert compile_dify_yaml(result.yaml).code.warnings == ()


@pytest.mark.parametrize(
    "description",
    [
        "workflow",
        "analysis",
        "paper",
        "content",
        "support",
        "工作流",
        "分析",
        "处理",
        "信息",
    ],
)
def test_generic_single_words_never_reuse_a_catalog_template(description: str) -> None:
    result = scaffold_workflow(description)

    assert result.origin == "generated"
    assert result.template_id is None


def test_explicit_template_selection_and_unknown_id() -> None:
    selected = scaffold_workflow("ignored description", template_id="executive-summary")

    assert selected.origin == "template"
    assert selected.template_id == "executive-summary"
    assert selected.matcher == "explicit"
    with pytest.raises(WorkflowTemplateNotFoundError):
        scaffold_workflow("ignored description", template_id="not-in-catalog")


@pytest.mark.parametrize("description", ["", "   ", "contains\x00nul"])
def test_invalid_descriptions_use_domain_error(description: str) -> None:
    with pytest.raises(WorkflowInputError):
        scaffold_workflow(description, reuse=False)


def test_description_length_is_bounded() -> None:
    with pytest.raises(WorkflowInputError, match="最长"):
        scaffold_workflow("x" * (MAX_DESCRIPTION_CHARS + 1), reuse=False)


def test_description_with_lone_surrogate_uses_domain_error() -> None:
    with pytest.raises(WorkflowInputError, match="Unicode|surrogate|编码"):
        scaffold_workflow("ok\ud800bad", reuse=False)


def test_generated_yaml_neutralizes_template_injection_without_losing_text() -> None:
    description = "中文\n---\n{{#env.SECRET#}} ${TOKEN}"

    first = scaffold_workflow(description, reuse=False)
    second = scaffold_workflow(description, reuse=False)
    parsed = parse_workflow(first.yaml, format="yaml")
    workflow = cast(dict[str, object], parsed["workflow"])
    graph = cast(dict[str, object], workflow["graph"])
    nodes = cast(list[object], graph["nodes"])
    llm = cast(dict[str, object], cast(dict[str, object], nodes[1])["data"])
    prompts = cast(list[object], llm["prompt_template"])
    system_prompt = cast(dict[str, object], prompts[0])["text"]

    assert first.yaml == second.yaml
    assert "{{#env.SECRET#}}" not in first.yaml
    assert "{ {#env.SECRET#} }" in cast(str, system_prompt)
    assert "中文" in cast(str, system_prompt)
    assert "---" in cast(str, system_prompt)
    assert "${TOKEN}" in cast(str, system_prompt)
    assert len(nodes) == 3
