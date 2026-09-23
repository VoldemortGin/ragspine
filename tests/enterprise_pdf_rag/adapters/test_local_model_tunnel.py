"""SSH tunnel configuration never embeds deployment addresses or credentials."""

from pathlib import Path

import pytest

from ragspine.common.evidence.providers.local_model_tunnel import (
    TunnelConfigurationError,
    TunnelStateError,
    load_tunnel_config,
    stop_tunnel,
    tunnel_status,
)


def test_tunnel_command_forwards_both_services_on_loopback() -> None:
    config = load_tunnel_config(
        {
            "LOCAL_MODELS_SSH_HOST": "operator@gpu.example",
            "LOCAL_MODELS_SSH_PORT": "6000",
            "EMBEDDING_LOCAL_PORT": "39002",
            "EMBEDDING_REMOTE_PORT": "28002",
            "RERANK_LOCAL_PORT": "39001",
            "RERANK_REMOTE_PORT": "28001",
        }
    )

    assert config.ssh_command == (
        "/usr/bin/ssh",
        "-N",
        "-T",
        "-p",
        "6000",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        "127.0.0.1:39002:127.0.0.1:28002",
        "-L",
        "127.0.0.1:39001:127.0.0.1:28001",
        "--",
        "operator@gpu.example",
    )
    assert config.embedding_base_url == "http://127.0.0.1:39002"
    assert config.rerank_base_url == "http://127.0.0.1:39001"


def test_tunnel_configuration_rejects_shell_like_host_and_reused_port() -> None:
    base = {
        "LOCAL_MODELS_SSH_HOST": "operator@gpu.example",
        "LOCAL_MODELS_SSH_PORT": "6000",
        "EMBEDDING_LOCAL_PORT": "39002",
        "EMBEDDING_REMOTE_PORT": "28002",
        "RERANK_LOCAL_PORT": "39001",
        "RERANK_REMOTE_PORT": "28001",
    }
    with pytest.raises(TunnelConfigurationError, match="SSH host"):
        load_tunnel_config(base | {"LOCAL_MODELS_SSH_HOST": "host; command"})
    with pytest.raises(TunnelConfigurationError, match="local ports must differ"):
        load_tunnel_config(base | {"RERANK_LOCAL_PORT": "39002"})


def test_status_and_stop_are_idempotent_when_no_record_exists(tmp_path: Path) -> None:
    config = load_tunnel_config(
        {
            "LOCAL_MODELS_SSH_HOST": "operator@gpu.example",
            "LOCAL_MODELS_SSH_PORT": "6000",
            "EMBEDDING_LOCAL_PORT": "39002",
            "EMBEDDING_REMOTE_PORT": "28002",
            "RERANK_LOCAL_PORT": "39001",
            "RERANK_REMOTE_PORT": "28001",
        }
    )

    status = tunnel_status(config, tmp_path)
    stopped = stop_tunnel(config, tmp_path)

    assert not status.running
    assert status.pid is None
    assert stopped == status


def test_invalid_pid_record_is_never_treated_as_stopped(tmp_path: Path) -> None:
    config = load_tunnel_config(
        {
            "LOCAL_MODELS_SSH_HOST": "operator@gpu.example",
            "LOCAL_MODELS_SSH_PORT": "6000",
            "EMBEDDING_LOCAL_PORT": "39002",
            "EMBEDDING_REMOTE_PORT": "28002",
            "RERANK_LOCAL_PORT": "39001",
            "RERANK_REMOTE_PORT": "28001",
        }
    )
    (tmp_path / "tunnel.pid").write_text("0\n", encoding="ascii")

    with pytest.raises(TunnelStateError, match="invalid"):
        stop_tunnel(config, tmp_path)
