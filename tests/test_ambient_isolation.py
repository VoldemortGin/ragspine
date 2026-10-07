"""钉住测试进程的隔离:真实 provider key / 代理变量不可见,对非回环地址 connect 会被拦。"""

import os
import socket

import pytest

_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_ORG_ID",
    "DEEPSEEK_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    "COHERE_API_KEY",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@pytest.mark.parametrize("name", _NAMES)
def test_ambient_secret_and_proxy_variables_are_absent(name):
    assert name not in os.environ


def test_connect_to_a_non_loopback_address_is_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(RuntimeError, match=r"BLOCKED_OUTBOUND host=203\.0\.113\.7 port=80"):
            s.connect(("203.0.113.7", 80))
        with pytest.raises(RuntimeError, match="BLOCKED_OUTBOUND"):
            s.connect_ex(("203.0.113.7", 80))


def test_create_connection_to_a_hostname_is_blocked():
    with pytest.raises(RuntimeError, match=r"BLOCKED_OUTBOUND host=example\.com port=443"):
        socket.create_connection(("example.com", 443))


def test_loopback_connect_is_allowed():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2):
            pass
