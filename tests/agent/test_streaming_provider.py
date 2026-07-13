"""StreamingProvider 缝 + MockProvider.chat_stream 单元测试。

验证：MockProvider 满足 StreamingProvider；chat_stream 与 chat 内容等价且确定；
纯 chat-only 桩不满足协议；iter_text_chunks 空串无 chunk、非空串可无损重组。
"""

import os
from datetime import date

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import (
    NARRATIVE_PROMPT_PREFIX,
    STREAM_CHUNK_CHARS,
    MockProvider,
    StreamingProvider,
    iter_text_chunks,
)

REF_DATE = date(2026, 6, 12)


class _ChatOnlyStub:
    """只有 chat、无 chat_stream —— 不应满足 StreamingProvider。"""

    def chat(self, messages, *, tools=None):
        raise AssertionError("not called")


def _narrative_messages() -> list[dict[str, object]]:
    return [{"role": "user", "content": f"{NARRATIVE_PROMPT_PREFIX}香港REVENUE同比增长因产品组合改善。"}]


def test_mockprovider_is_streaming_provider():
    mp = MockProvider(reference_date=REF_DATE)
    assert isinstance(mp, StreamingProvider)


def test_chat_only_stub_is_not_streaming_provider():
    assert not isinstance(_ChatOnlyStub(), StreamingProvider)


def test_chat_stream_equals_chat_content():
    mp = MockProvider(reference_date=REF_DATE)
    messages = _narrative_messages()
    streamed = "".join(mp.chat_stream(messages))
    expected = mp.chat(messages).choices[0].message.content or ""
    assert streamed == expected
    assert streamed  # 叙事路径确有内容


def test_chat_stream_is_deterministic():
    mp = MockProvider(reference_date=REF_DATE)
    messages = _narrative_messages()
    first = "".join(mp.chat_stream(messages))
    second = "".join(mp.chat_stream(messages))
    assert first == second


def test_iter_text_chunks_empty_yields_nothing():
    assert list(iter_text_chunks("")) == []


def test_iter_text_chunks_roundtrips_nontrivial_string():
    text = "香港 FY2025 REVENUE 为 1702 USD_M（来源：X · Y）" * 3
    chunks = list(iter_text_chunks(text))
    assert len(chunks) > 1
    assert all(len(c) <= STREAM_CHUNK_CHARS for c in chunks)
    assert "".join(chunks) == text
