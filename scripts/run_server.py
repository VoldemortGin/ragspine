"""FastAPI 服务启动入口：uvicorn 跑 create_app()（配置由环境变量注入）。

用法（从项目根目录）：
    RAGSPINE_DB_PATH=data/fact_metric.db \\
    RAGSPINE_CHUNK_DB_PATH=data/narrative.db \\
    .venv/bin/python scripts/run_server.py --host 0.0.0.0 --port 8000

配置全部走环境变量（见 ragspine/service/config.py 的 ServiceConfig.from_env）：
RAGSPINE_DB_PATH / RAGSPINE_CHUNK_DB_PATH / RAGSPINE_PROVIDER(mock|anthropic) / RAGSPINE_MODEL / RAGSPINE_BASE_URL /
RAGSPINE_FAQ_SOURCE / RAGSPINE_ALLOWED_UPLOAD_ROOT / RAGSPINE_REDIS_URL 等。
依赖：pip install -e ".[service]"。
"""

import argparse
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAGSpine HTTP 服务（FastAPI + uvicorn）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认本机）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    args = parser.parse_args(argv)

    import uvicorn

    app = create_app(ServiceConfig.from_env())
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
