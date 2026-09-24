"""跨语言查询翻译（RAGSPINE_QUERY_TRANSLATION）的共享替身：英文按页语料 + 可编排的翻译 provider。"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ProviderError

from ragspine.agent.llm_provider import _completion_text
from ragspine.retrieval.chunking.chunk_store import ChunkStore
from tests.retrieval.contextual_index.conftest import heading_corpus, load_heading_corpus

__all__ = ["heading_corpus", "load_heading_corpus", "TranslatingProvider"]


class TranslatingProvider:
    """按 user 消息查表翻译；记录每次调用的 messages。fail=True 时抛 ProviderError。"""

    def __init__(self, table: dict[str, str] | None = None, *, fail: bool = False) -> None:
        self.table = table or {}
        self.fail = fail
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, tools=None):
        self.calls.append(messages)
        if self.fail:
            raise ProviderError("down")
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        return _completion_text(self.table.get(user, ""))


@pytest.fixture
def en_store(tmp_path):
    store = ChunkStore(tmp_path / "chunks.db")
    load_heading_corpus(store)
    yield store
    store.close()
