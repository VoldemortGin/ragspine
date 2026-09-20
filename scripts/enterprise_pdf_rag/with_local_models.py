"""Exec one command with in-memory local-model credentials and loopback endpoints."""

import argparse
import os
import sys
from pathlib import Path

from enterprise_pdf_rag.adapters.local_model_launcher import (
    build_local_model_child_environment,
    read_remote_container_api_key,
)
from enterprise_pdf_rag.adapters.local_model_tunnel import (
    load_tunnel_config,
    tunnel_status,
)

ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "data" / "local-models"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
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
    embedding_key = read_remote_container_api_key(tunnel, _required("EMBEDDING_REMOTE_CONTAINER"))
    rerank_key = read_remote_container_api_key(tunnel, _required("RERANK_REMOTE_CONTAINER"))
    child_environment = build_local_model_child_environment(
        os.environ,
        tunnel,
        embedding_api_key=embedding_key,
        rerank_api_key=rerank_key,
    )
    os.execvpe(command[0], command, child_environment)


if __name__ == "__main__":
    sys.exit(main())
