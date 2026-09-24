"""``RAGSpine.open_retriever()``：与 ``ask`` 同一套守卫与检索组装（批量 retrieval-only 评测的入口）。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ragspine.agent.llm_provider import LLMProvider, MockProvider
from ragspine.service.config import ServiceConfig

_DECK = """# Fictional Deck

Welcome to the results.

<!-- PageBreak -->

# Distribution Mix

Agency share of VONB was 72%. Partnerships share of VONB was 28%.
"""


def _ingested(tmp_path: Path) -> Path:
    from ragspine import RAGSpine

    workspace = tmp_path / "ws"
    deck = tmp_path / "deck.md"
    deck.write_text(_DECK, encoding="utf-8")
    RAGSpine.local(workspace).ingest(deck)
    return workspace


def test_open_retriever_retrieves_from_the_workspace(tmp_path):
    from ragspine import RAGSpine

    workspace = _ingested(tmp_path)
    with RAGSpine.local(workspace) as rag, rag.open_retriever() as retriever:
        assert retriever is not None
        hits = retriever.retrieve("Agency share of VONB", top_k=5)
    assert hits and "@page=2" in str(hits[0]["source_locator"])


def test_open_retriever_keeps_the_ask_guards(tmp_path):
    from ragspine import RAGSpine
    from ragspine.config import ReindexRequiredError

    workspace = _ingested(tmp_path)
    rag = RAGSpine.local(workspace)
    rag.close()
    with pytest.raises(RuntimeError, match="closed"), rag.open_retriever():
        pass

    other = {"indexing": {"chunker": "parent_child", "max_chars": 16, "overlap_chars": 0}}
    with (
        RAGSpine.local(workspace, config=other) as mismatched,
        pytest.raises(ReindexRequiredError),
        mismatched.open_retriever(),
    ):
        pass


@pytest.mark.parametrize(
    ("preset", "config", "expected"),
    [
        (None, None, {"embedding": "none", "reranker": "none", "persist_vectors": False}),
        (
            "balanced",
            {
                "retrieval": {"embedding": "local-http", "reranker": "local-http"},
                "storage": {"persist_vectors": True},
            },
            {"embedding": "local-http", "reranker": "local-http", "persist_vectors": True},
        ),
    ],
)
def test_open_retriever_and_ask_share_one_service_config(
    tmp_path, monkeypatch, preset, config, expected
):
    """open_retriever 与 ask 组装检索器用的是同一个 ServiceConfig + 同一个 provider。

    local-http（Qwen embedding / reranker）只验证配置透传，桩掉真正的组装，不连模型服务。
    """
    from ragspine import RAGSpine

    seen: list[tuple[ServiceConfig, LLMProvider]] = []

    @contextmanager
    def fake_open(config: ServiceConfig, provider: LLMProvider) -> Iterator[None]:
        seen.append((config, provider))
        yield None

    monkeypatch.setattr("ragspine.session.open_narrative_retriever", fake_open)
    provider = MockProvider()
    with RAGSpine.local(tmp_path / "ws", provider=provider, preset=preset, config=config) as rag:
        rag.retrieval = rag.retrieval.with_overrides(
            contextual_index="heading", query_translation="off"
        )
        with rag.open_retriever() as retriever:
            assert retriever is None
        rag.ask("Agency share of VONB")

    assert len(seen) == 2
    (retriever_config, retriever_provider), (ask_config, ask_provider) = seen
    assert retriever_config == ask_config
    assert retriever_provider is ask_provider is provider
    for field, value in expected.items():
        assert getattr(retriever_config, field) == value
    assert retriever_config.contextual_index == "heading"
    assert retriever_config.query_translation == "off"
