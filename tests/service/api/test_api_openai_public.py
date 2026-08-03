"""服务层 /v1/chat/completions（OpenAI Chat Completions API 形状克隆）HTTP 行为测试。

验证对外形状与 OpenAI 官方一致（现有 openai SDK / Open WebUI / LangChain 等客户端零改动
直连本服务）：blocking / stream 两种模式、messages→question+history 映射、官方错误体、
/v1/models 列表。溯源不丢：非标准的 `ragspine` 扩展字段承载 route + sources（OpenAI 客户端
忽略未知字段，故不破坏兼容）。注入 MockProvider + FakeQueue，零真实 LLM API。
"""

import json
import os

import pytest
import rootutils
from fastapi.testclient import TestClient

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.tasks.task_queue import FakeQueue


@pytest.fixture
def client(tmp_path) -> TestClient:
    config = ServiceConfig(db_path=str(tmp_path / "fact.db"))
    app = create_app(config, provider=MockProvider(), queue=FakeQueue())
    return TestClient(app)


def _body(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": "ragspine",
        "messages": [{"role": "user", "content": "公司去年营收情况如何？"}],
    }
    payload.update(overrides)
    return payload


def test_blocking_response_has_openai_chat_completion_shape(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=_body())

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert isinstance(body["created"], int)
    assert body["model"] == "ragspine"
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["message"]["role"] == "assistant"
    assert isinstance(choice["message"]["content"], str)
    assert choice["finish_reason"] == "stop"
    usage = body["usage"]
    assert {"prompt_tokens", "completion_tokens", "total_tokens"} <= set(usage)


def test_provenance_survives_in_ragspine_extension_field(client: TestClient) -> None:
    """溯源不得因套上 OpenAI 形状而丢失——route/sources 走扩展字段带出。"""
    body = client.post("/v1/chat/completions", json=_body()).json()

    extension = body["ragspine"]
    assert isinstance(extension["route"], str) and extension["route"]
    assert isinstance(extension["sources"], list)
    assert isinstance(extension["request_id"], str) and extension["request_id"]


def test_history_is_taken_from_earlier_messages(client: TestClient) -> None:
    """多轮 messages 映射为 question(最后一条 user) + history(此前轮次)。"""
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]
    response = client.post("/v1/chat/completions", json=_body(messages=messages))

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["role"] == "assistant"


def test_streaming_emits_openai_chunks_then_done(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=_body(stream=True))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in response.text.split("\n\n") if line.strip()]
    assert frames[-1].strip() == "data: [DONE]"

    payloads = [json.loads(frame.removeprefix("data: ")) for frame in frames[:-1]]
    assert all(item["object"] == "chat.completion.chunk" for item in payloads)
    assert payloads[0]["choices"][0]["delta"]["role"] == "assistant"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    streamed = "".join(item["choices"][0]["delta"].get("content", "") for item in payloads)
    assert streamed


def test_streaming_carries_provenance_on_final_chunk(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=_body(stream=True))

    frames = [line for line in response.text.split("\n\n") if line.strip()]
    final = json.loads(frames[-2].removeprefix("data: "))
    assert isinstance(final["ragspine"]["sources"], list)
    assert final["ragspine"]["route"]


def test_empty_messages_returns_openai_error_shape(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json=_body(messages=[]))

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["message"]
    assert error["param"] == "messages"


def test_messages_without_user_turn_is_rejected(client: TestClient) -> None:
    messages = [{"role": "system", "content": "只有系统提示"}]
    response = client.post("/v1/chat/completions", json=_body(messages=messages))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_models_endpoint_lists_ragspine(client: TestClient) -> None:
    response = client.get("/v1/models")

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    entry = body["data"][0]
    assert entry["id"] == "ragspine"
    assert entry["object"] == "model"
    assert entry["owned_by"] == "ragspine"


def test_answers_are_deterministic_across_calls(client: TestClient) -> None:
    """同问题两次调用答案一致（MockProvider 离线确定性）。"""
    first = client.post("/v1/chat/completions", json=_body()).json()
    second = client.post("/v1/chat/completions", json=_body()).json()

    assert first["choices"][0]["message"]["content"] == second["choices"][0]["message"]["content"]
