"""Manage this project's loopback-only SSH tunnel to local model services."""

import argparse
import sys
from pathlib import Path

from enterprise_pdf_rag.adapters.local_model_tunnel import (
    load_tunnel_config,
    start_tunnel,
    stop_tunnel,
    tunnel_status,
)

ROOT = Path(__file__).resolve().parent.parent.parent
STATE = ROOT / "data" / "local-models"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    args = parser.parse_args()
    config = load_tunnel_config()
    if args.action == "start":
        status = start_tunnel(config, STATE, cwd=ROOT)
    elif args.action == "stop":
        status = stop_tunnel(config, STATE)
    else:
        status = tunnel_status(config, STATE)
    print(status.model_dump_json(indent=2))
    return 0 if status.running or args.action == "stop" else 1


if __name__ == "__main__":
    sys.exit(main())
