"""Deterministic lexical and injectable semantic workflow-template matching."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ragspine.retrieval.lexical.retrieval import (
    EmbeddingBackend,
    bm25_scores,
    cosine_similarity,
    tokenize,
)
from ragspine.workflows.errors import WorkflowMatcherError
from ragspine.workflows.model import TemplateMatch, WorkflowTemplate

_GENERIC_QUERY_TOKENS = frozenset(
    {
        "a",
        "an",
        "analysis",
        "analyze",
        "and",
        "build",
        "content",
        "create",
        "flow",
        "for",
        "make",
        "of",
        "paper",
        "please",
        "support",
        "the",
        "to",
        "workflow",
    }
).union(
    token
    for phrase in ("工作流", "分析", "处理", "信息", "内容", "支持", "创建", "构建", "论文")
    for token in tokenize(phrase)
)


@runtime_checkable
class TemplateMatcher(Protocol):
    """Rank a natural-language request against catalog metadata."""

    name: str
    reuse_threshold: float
    reuse_margin: float

    def rank(
        self, query: str, templates: Sequence[WorkflowTemplate]
    ) -> tuple[TemplateMatch, ...]: ...


class LexicalTemplateMatcher:
    """Offline BM25 + token-coverage matcher with deterministic tie-breaking."""

    name = "lexical"
    reuse_threshold = 0.74
    reuse_margin = 0.08

    def rank(self, query: str, templates: Sequence[WorkflowTemplate]) -> tuple[TemplateMatch, ...]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return ()
        docs_tokens = [tokenize(template.search_text) for template in templates]
        raw_scores = bm25_scores(query_tokens, docs_tokens)
        max_bm25 = max(raw_scores, default=0.0)
        query_set = set(query_tokens)
        normalized_query = " ".join(query.lower().split())

        matches: list[TemplateMatch] = []
        for template, doc_tokens, bm25 in zip(templates, docs_tokens, raw_scores, strict=True):
            overlap_tokens = query_set.intersection(doc_tokens)
            overlap = len(overlap_tokens) / len(query_set)
            bm25_part = bm25 / max_bm25 if max_bm25 > 0 else 0.0
            normalized_examples = {
                " ".join(item.lower().split())
                for item in (template.name, *template.examples)
            }
            exact = normalized_query in normalized_examples
            confidence = 1.0 if exact else min(1.0, 0.62 * overlap + 0.38 * bm25_part)
            informative_overlap = overlap_tokens.difference(_GENERIC_QUERY_TOKENS)
            if not exact and len(informative_overlap) < 2:
                # A corpus-relative BM25 leader is not semantic certainty: a single generic
                # token ("paper", "analysis", "workflow", …) must never trigger reuse.
                confidence = min(confidence, self.reuse_threshold - 0.01)
            matches.append(
                TemplateMatch(
                    template=template,
                    confidence=confidence,
                    matcher=self.name,
                )
            )
        matches.sort(key=lambda match: (-match.confidence, match.template.id))
        return tuple(matches)


class EmbeddingTemplateMatcher:
    """True semantic cosine matcher over public catalog metadata.

    The embedding backend is injected.  No SDK or model is selected by this
    class, keeping tests deterministic and model/network use explicit.
    """

    name = "embedding"
    reuse_threshold = 0.82
    reuse_margin = 0.05

    def __init__(self, backend: EmbeddingBackend, *, name: str = "embedding") -> None:
        self._backend = backend
        self.name = name

    def rank(self, query: str, templates: Sequence[WorkflowTemplate]) -> tuple[TemplateMatch, ...]:
        try:
            if not templates:
                return ()
            texts = [query, *(template.search_text for template in templates)]
            vectors = self._backend.embed_texts(texts)
            if len(vectors) != len(texts):
                raise WorkflowMatcherError("embedding 返回条数不正确")
            query_vector = vectors[0]
            if not query_vector:
                raise WorkflowMatcherError("embedding 返回空 query 向量")

            matches: list[TemplateMatch] = []
            for template, vector in zip(templates, vectors[1:], strict=True):
                if len(vector) != len(query_vector) or not vector:
                    raise WorkflowMatcherError("embedding 维度不一致或为空")
                if not all(math.isfinite(value) for value in (*query_vector, *vector)):
                    raise WorkflowMatcherError("embedding 含 NaN/Inf")
                confidence = max(0.0, min(1.0, cosine_similarity(query_vector, vector)))
                matches.append(
                    TemplateMatch(
                        template=template,
                        confidence=confidence,
                        matcher=self.name,
                    )
                )
            matches.sort(key=lambda match: (-match.confidence, match.template.id))
            return tuple(matches)
        except WorkflowMatcherError:
            raise
        except Exception as exc:
            raise WorkflowMatcherError("semantic embedding matcher 不可用") from exc


def make_template_matcher(spec: str = "auto") -> TemplateMatcher:
    """Build a workflow matcher without loading a model or touching the network."""

    from ragspine.retrieval.vector.embedding_backends import make_embedding_backend

    normalized = spec.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"none", "lexical"}:
        return LexicalTemplateMatcher()
    if normalized not in {"auto", "onnx"}:
        raise WorkflowMatcherError("workflow matcher 只支持 auto/none/onnx")
    try:
        backend = make_embedding_backend(normalized)
    except Exception as exc:
        raise WorkflowMatcherError("semantic embedding matcher 初始化失败") from exc
    if backend is None:
        if normalized == "auto":
            return LexicalTemplateMatcher()
        raise WorkflowMatcherError("onnx workflow matcher 不可用")
    return EmbeddingTemplateMatcher(backend, name="onnx")


def choose_reusable(
    matches: Sequence[TemplateMatch],
    *,
    threshold: float,
    margin: float,
) -> TemplateMatch | None:
    """Choose only a high-confidence leader; ambiguity always generates anew."""

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold 必须在 0..1")
    if not 0.0 <= margin <= 1.0:
        raise ValueError("margin 必须在 0..1")
    if not matches:
        return None
    first = matches[0]
    runner_up = matches[1].confidence if len(matches) > 1 else 0.0
    if first.confidence < threshold or first.confidence - runner_up < margin:
        return None
    return first


__all__ = [
    "TemplateMatcher",
    "LexicalTemplateMatcher",
    "EmbeddingTemplateMatcher",
    "make_template_matcher",
    "choose_reusable",
]
