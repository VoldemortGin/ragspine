"""The launcher injects only required secrets into one controlled child."""

from pydantic import SecretStr

from ragspine.common.evidence.providers.local_model_launcher import (
    build_local_model_child_environment,
    read_remote_container_api_key,
)
from ragspine.common.evidence.providers.local_model_tunnel import load_tunnel_config


def test_child_environment_is_whitelisted_and_contains_no_deployment_fallback() -> None:
    tunnel = load_tunnel_config(
        {
            "LOCAL_MODELS_SSH_HOST": "operator@gpu.example",
            "LOCAL_MODELS_SSH_PORT": "6000",
            "EMBEDDING_LOCAL_PORT": "39002",
            "EMBEDDING_REMOTE_PORT": "28002",
            "RERANK_LOCAL_PORT": "39001",
            "RERANK_REMOTE_PORT": "28001",
        }
    )
    child = build_local_model_child_environment(
        {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/operator",
            "OPENAI_API_KEY": "cloud-secret",
            "OPENAI_BASE_URL": "https://provider.example",
            "OPENAI_MODEL": "configured-model",
            "EMBEDDING_MODEL": "embedding-model",
            "RERANK_MODEL": "rerank-model",
            "AWS_SECRET_ACCESS_KEY": "must-not-inherit",
        },
        tunnel,
        embedding_api_key=SecretStr("embedding-secret"),
        rerank_api_key=SecretStr("rerank-secret"),
    )

    assert child == {
        "HOME": "/home/operator",
        "OPENAI_API_KEY": "cloud-secret",
        "OPENAI_BASE_URL": "https://provider.example",
        "OPENAI_MODEL": "configured-model",
        "PATH": "/usr/bin:/bin",
        "PYTHON_DOTENV_DISABLED": "1",
        "EMBEDDING_API_KEY": "embedding-secret",
        "EMBEDDING_BASE_URL": "http://127.0.0.1:39002",
        "EMBEDDING_MODEL": "embedding-model",
        "RERANK_API_KEY": "rerank-secret",
        "RERANK_BASE_URL": "http://127.0.0.1:39001",
        "RERANK_MODEL": "rerank-model",
    }
    assert "AWS_SECRET_ACCESS_KEY" not in child


def test_remote_key_reader_returns_secret_without_placing_it_in_ssh_args() -> None:
    tunnel = load_tunnel_config(
        {
            "LOCAL_MODELS_SSH_HOST": "operator@gpu.example",
            "LOCAL_MODELS_SSH_PORT": "6000",
            "EMBEDDING_LOCAL_PORT": "39002",
            "EMBEDDING_REMOTE_PORT": "28002",
            "RERANK_LOCAL_PORT": "39001",
            "RERANK_REMOTE_PORT": "28001",
        }
    )
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...]) -> str:
        commands.append(command)
        return "service-secret\n"

    secret = read_remote_container_api_key(tunnel, "embedding-service", runner=runner)

    assert secret.get_secret_value() == "service-secret"
    assert commands == [
        (
            "/usr/bin/ssh",
            "-T",
            "-p",
            "6000",
            "-o",
            "BatchMode=yes",
            "--",
            "operator@gpu.example",
            "docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' -- embedding-service | sed -n 's/^VLLM_API_KEY=//p'",
        )
    ]
    assert "service-secret" not in " ".join(commands[0])
