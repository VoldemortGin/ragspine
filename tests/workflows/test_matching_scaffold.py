"""Deterministic matching and safe workflow scaffolding contracts."""

from __future__ import annotations

import math
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError, Event, Lock
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
from ragspine.workflows.model import TemplateMatch, WorkflowTemplate
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


def test_lexical_matcher_aligns_bilingual_catalog_taxonomy() -> None:
    matcher = LexicalTemplateMatcher()
    templates = load_builtin_catalog().runnable()
    cases = (
        (
            "发票自动识别、抽取金额税额并审核",
            None,
            {"industry:accounting", "use-case:invoice-processing"},
        ),
        (
            "Extract and validate invoice number tax amount and vendor",
            None,
            {"industry:accounting", "use-case:invoice-processing"},
        ),
        (
            "法律研究助手：根据案例和法规形成带证据的研究报告",
            None,
            {"industry:legal", "use-case:research"},
        ),
        (
            "Legal research agent for contracts and case law",
            None,
            {"industry:legal", "use-case:research"},
        ),
        (
            "客服工单按紧急程度分类并路由到对应团队",
            "conditional-response-routing",
            set(),
        ),
        (
            "每天汇总人工智能新闻生成摘要",
            None,
            {"industry:media", "use-case:summarization"},
        ),
        (
            "Create a daily AI news digest and evidence summary",
            None,
            {"industry:media", "use-case:summarization"},
        ),
        (
            "预约客户会议，基于日历可用时间推荐时段",
            None,
            {"use-case:scheduling"},
        ),
        (
            "Schedule a patient appointment using calendar availability",
            None,
            {"industry:healthcare", "use-case:scheduling"},
        ),
        (
            "监控竞争对手价格变化并产生信号报告",
            None,
            {"use-case:monitoring"},
        ),
        (
            "天气预报",
            None,
            {"industry:environment"},
        ),
        (
            "智能知识库客服问答",
            None,
            {"use-case:knowledge-retrieval"},
        ),
        (
            "Publish a marketing campaign post to social media",
            None,
            {"use-case:social-publishing"},
        ),
        (
            "Cybersecurity incident alert and severity triage",
            None,
            {"industry:cybersecurity", "use-case:alerting"},
        ),
    )

    for query, expected_id, expected_categories in cases:
        leader = matcher.rank(query, templates)[0].template
        if expected_id is not None:
            assert leader.id == expected_id, query
        assert expected_categories.issubset(leader.categories), query


def test_equivalent_catalog_candidates_do_not_block_safe_reuse() -> None:
    matcher = LexicalTemplateMatcher()

    weather = scaffold_workflow("Weather forecast workflow", matcher=matcher)
    extraction = scaffold_workflow("Extract structured fields", matcher=matcher)

    assert weather.origin == "template"
    assert weather.template_id is not None
    assert weather.template_id.startswith("dify-environment-general-assistance-weather-")
    assert extraction.origin == "template"
    assert extraction.template_id == "structured-information-extraction"


@pytest.mark.parametrize(
    "query",
    [
        "Monitor website uptime and alert on failures",
        "招聘简历筛选和候选人分类",
        "酒店预订客服助手",
        "Publish a marketing campaign post to social media",
    ],
)
def test_cross_function_or_cross_industry_partial_matches_generate(query: str) -> None:
    result = scaffold_workflow(query)

    assert result.origin == "generated"
    assert result.template_id is None


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


def test_embedding_matcher_reuses_catalog_vectors_across_queries() -> None:
    class RecordingBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(tuple(texts))
            if len(texts) == 1:
                return [[1.0, 0.0]]
            return [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]

    backend = RecordingBackend()
    templates = load_builtin_catalog().runnable()[:2]
    matcher = EmbeddingTemplateMatcher(backend)

    first = matcher.rank("first semantic query", templates)
    second = matcher.rank("second semantic query", templates)

    assert first[0].template.id == second[0].template.id == templates[0].id
    assert [len(call) for call in backend.calls] == [3, 1]


def test_embedding_catalog_encoding_is_single_flight_across_concurrent_queries() -> None:
    class BlockingBackend:
        def __init__(self) -> None:
            self.calls: list[int] = []
            self.started = Event()
            self.release = Event()
            self.lock = Lock()

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            with self.lock:
                self.calls.append(len(texts))
            if len(texts) > 1:
                self.started.set()
                assert self.release.wait(timeout=10)
                return [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
            return [[1.0, 0.0]]

    backend = BlockingBackend()
    matcher = EmbeddingTemplateMatcher(backend)
    templates = load_builtin_catalog().runnable()[:2]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(matcher.rank, "first query", templates)
        assert backend.started.wait(timeout=10)
        second = pool.submit(matcher.rank, "second query", templates)
        backend.release.set()
        first_matches = first.result(timeout=20)
        second_matches = second.result(timeout=20)

    assert first_matches[0].template.id == second_matches[0].template.id
    assert sorted(backend.calls) == [1, 3]


def test_embedding_single_flight_failure_does_not_poison_retry() -> None:
    class RetryBackend:
        def __init__(self) -> None:
            self.calls = 0

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("sk-proj-backend-error-must-not-leak")
            return [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]

    backend = RetryBackend()
    matcher = EmbeddingTemplateMatcher(backend)
    templates = load_builtin_catalog().runnable()[:2]

    with pytest.raises(WorkflowMatcherError) as error:
        matcher.rank("first query", templates)
    matches = matcher.rank("retry query", templates)

    assert "sk-proj-backend-error" not in str(error.value)
    assert backend.calls == 2
    assert matches[0].template.id == templates[0].id


def test_scaffold_matches_metadata_refs_then_clones_only_selected_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ragspine.workflows import catalog as catalog_module

    catalog = catalog_module.WorkflowCatalog(load_builtin_catalog().list()[:2])
    real_clone = catalog_module._clone_template
    clone_calls = 0

    class MutatingMatcher:
        name = "metadata-only"
        reuse_threshold = 0.5
        reuse_margin = 0.0

        def rank(
            self,
            query: str,
            templates: Sequence[WorkflowTemplate],
        ) -> tuple[TemplateMatch, ...]:
            del query
            assert templates[0].yaml == ""
            assert templates[0].workflow == {}
            templates[0].workflow["attacker"] = "cannot reach catalog workflow"
            return (TemplateMatch(templates[0], confidence=1.0, matcher=self.name),)

    def recording_clone(template: WorkflowTemplate) -> WorkflowTemplate:
        nonlocal clone_calls
        clone_calls += 1
        return real_clone(template)

    monkeypatch.setattr(catalog_module, "_clone_template", recording_clone)

    result = scaffold_workflow("select first", catalog=catalog, matcher=MutatingMatcher())

    assert clone_calls == 1
    assert result.origin == "template"
    assert "app" in result.workflow
    assert "attacker" not in result.workflow


def test_lexical_matcher_reuses_catalog_tokenization_across_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ragspine.workflows import matching as matching_module

    real_tokenize = matching_module.tokenize
    calls = 0

    def recording_tokenize(text: str) -> list[str]:
        nonlocal calls
        calls += 1
        return real_tokenize(text)

    monkeypatch.setattr(matching_module, "tokenize", recording_tokenize)
    templates = load_builtin_catalog().runnable()[:2]
    matcher = LexicalTemplateMatcher()

    matcher.rank("first query", templates)
    matcher.rank("second query", templates)

    assert calls == 4


def test_lexical_catalog_tokenization_is_single_flight_and_lock_free() -> None:
    from ragspine.workflows import matching as matching_module

    templates = load_builtin_catalog().runnable()
    template_texts = {template.search_text for template in templates}
    first_template_text = templates[0].search_text
    real_tokenize = matching_module.tokenize
    matcher = LexicalTemplateMatcher()
    rendezvous = Barrier(2)
    count_lock = Lock()
    template_calls = 0
    tokenized_outside_lock: list[bool] = []

    def recording_tokenize(text: str) -> list[str]:
        nonlocal template_calls
        if text in template_texts:
            matcher_lock = getattr(matcher, "_template_token_lock", None)
            if matcher_lock is not None:
                acquired = matcher_lock.acquire(blocking=False)
                tokenized_outside_lock.append(acquired)
                if acquired:
                    matcher_lock.release()
            with count_lock:
                template_calls += 1
            if text == first_template_text:
                try:
                    rendezvous.wait(timeout=0.5)
                except BrokenBarrierError:
                    pass
        return real_tokenize(text)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(matching_module, "tokenize", recording_tokenize)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(matcher.rank, "first concurrent query", templates)
            second = pool.submit(matcher.rank, "second concurrent query", templates)
            first_matches = first.result(timeout=20)
            second_matches = second.result(timeout=20)

    assert first_matches
    assert second_matches
    assert template_calls == len(templates) == 1000
    assert tokenized_outside_lock and all(tokenized_outside_lock)


def test_lexical_single_flight_failure_does_not_poison_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ragspine.workflows import matching as matching_module

    templates = load_builtin_catalog().runnable()[:2]
    first_template_text = templates[0].search_text
    real_tokenize = matching_module.tokenize
    failed = False
    template_calls = 0

    def fail_once(text: str) -> list[str]:
        nonlocal failed, template_calls
        if text == first_template_text:
            template_calls += 1
            if not failed:
                failed = True
                raise RuntimeError("transient tokenizer failure")
        return real_tokenize(text)

    monkeypatch.setattr(matching_module, "tokenize", fail_once)
    matcher = LexicalTemplateMatcher()

    with pytest.raises(RuntimeError, match="transient tokenizer failure"):
        matcher.rank("first query", templates)
    matches = matcher.rank("retry query", templates)

    assert template_calls == 2
    assert matches


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
