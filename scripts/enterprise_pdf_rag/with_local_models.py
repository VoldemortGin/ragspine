"""Exec one command with in-memory local-model credentials and loopback endpoints."""

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from ragspine.common.evidence.configs import Settings
from ragspine.common.evidence.providers.local_model_launcher import (
    build_local_model_child_environment,
    read_remote_container_api_key,
)
from ragspine.common.evidence.providers.local_model_tunnel import (
    load_tunnel_config,
    tunnel_status,
)

ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "data" / "local-models"
# Settings the parent resolves (environment > project-root .env) before building the child.
_SETTINGS = (
    "APP_LLM_API_KEY",
    "APP_LLM_BASE_URL",
    "APP_LLM_MODEL",
    "APP_EMBEDDING_MODEL",
    "APP_RERANK_MODEL",
    "APP_TUNNEL_EMBEDDING_REMOTE_CONTAINER",
    "APP_TUNNEL_RERANK_REMOTE_CONTAINER",
)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value or "\n" in value or "\r" in value:
        raise SystemExit(f"Missing or invalid environment setting: {name}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command: list[str] = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("A child command is required after --")
    tunnel = load_tunnel_config()
    if not tunnel_status(tunnel, STATE).running:
        raise SystemExit("The configured project tunnel is not running")
    environment = {**os.environ, **Settings().as_environment(_SETTINGS)}
    embedding_key = read_remote_container_api_key(
        tunnel, _required(environment, "APP_TUNNEL_EMBEDDING_REMOTE_CONTAINER")
    )
    rerank_key = read_remote_container_api_key(
        tunnel, _required(environment, "APP_TUNNEL_RERANK_REMOTE_CONTAINER")
    )
    child_environment = build_local_model_child_environment(
        environment,
        tunnel,
        embedding_api_key=embedding_key,
        rerank_api_key=rerank_key,
    )
    os.execvpe(command[0], command, child_environment)


if __name__ == "__main__":
    sys.exit(main())
