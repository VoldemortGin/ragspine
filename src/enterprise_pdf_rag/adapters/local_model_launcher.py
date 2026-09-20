"""Inject remote model credentials into one allowlisted child environment."""

import re
import subprocess
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from enterprise_pdf_rag.adapters.local_model_tunnel import LocalModelTunnelConfig
from enterprise_pdf_rag.adapters.providers import ProviderConfigurationError

_INHERITED = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "USER",
)
_CONTAINER = re.compile(r"[A-Za-z0-9_.-]+")


@runtime_checkable
class SshRunner(Protocol):
    def __call__(self, command: tuple[str, ...]) -> str: ...


def _run_ssh(command: tuple[str, ...]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ProviderConfigurationError(
            "Remote model credential lookup failed; no output retained"
        )
    return result.stdout


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value or "\n" in value or "\r" in value:
        raise ProviderConfigurationError(f"Missing or invalid environment setting: {name}")
    return value


def build_local_model_child_environment(
    parent_environment: Mapping[str, str],
    tunnel: LocalModelTunnelConfig,
    *,
    embedding_api_key: SecretStr,
    rerank_api_key: SecretStr,
) -> dict[str, str]:
    """Return the complete environment for a single controlled child process."""

    child = {name: parent_environment[name] for name in _INHERITED if parent_environment.get(name)}
    child.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "EMBEDDING_API_KEY": embedding_api_key.get_secret_value(),
            "EMBEDDING_BASE_URL": tunnel.embedding_base_url,
            "EMBEDDING_MODEL": _required(parent_environment, "EMBEDDING_MODEL"),
            "RERANK_API_KEY": rerank_api_key.get_secret_value(),
            "RERANK_BASE_URL": tunnel.rerank_base_url,
            "RERANK_MODEL": _required(parent_environment, "RERANK_MODEL"),
        }
    )
    return child


def read_remote_container_api_key(
    tunnel: LocalModelTunnelConfig,
    container: str,
    *,
    runner: SshRunner | None = None,
) -> SecretStr:
    """Read one vLLM key through SSH; the key exists only in returned memory."""

    if not _CONTAINER.fullmatch(container) or container.startswith("-"):
        raise ProviderConfigurationError("Invalid remote model container name")
    remote_command = (
        "docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' -- "
        f"{container} | sed -n 's/^VLLM_API_KEY=//p'"
    )
    command = (
        "/usr/bin/ssh",
        "-T",
        "-p",
        str(tunnel.ssh_port),
        "-o",
        "BatchMode=yes",
        "--",
        tunnel.ssh_host,
        remote_command,
    )
    output = (runner if runner is not None else _run_ssh)(command).strip()
    if not output or "\n" in output or "\r" in output:
        raise ProviderConfigurationError("Remote model credential lookup returned no unique key")
    return SecretStr(output)
