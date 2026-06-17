"""RQ worker 启动入口：消费 ingestion job 队列（与 API 进程分离，可独立扩缩）。

用法（从项目根目录，需 Redis 可达）：
    RAGSPINE_REDIS_URL=redis://localhost:6379/0 .venv/bin/python scripts/run_worker.py

worker 自行打开/关闭每个 job 的 store/registry/queue（见 ragspine/service/tasks/jobs.py），
不与 API 进程共享 sqlite 连接。队列名须与 RQQueue 默认一致（ragspine-ingest）。
依赖：pip install -e ".[service]"。
"""

import argparse
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.service.config import ServiceConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAGSpine ingestion worker（RQ）")
    parser.add_argument(
        "--queue", default="ragspine-ingest", help="队列名（须与 RQQueue 默认一致）"
    )
    args = parser.parse_args(argv)

    config = ServiceConfig.from_env()

    from redis import Redis
    from rq import Queue, Worker

    conn = Redis.from_url(config.redis_url)
    worker = Worker([Queue(args.queue, connection=conn)], connection=conn)
    worker.work()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
