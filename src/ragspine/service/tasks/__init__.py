"""tasks —— 异步任务队列抽象 + worker 端 durable ingestion job。

队列经 TaskQueue 协议解耦：测试用 FakeQueue（同步内联），生产用 RQQueue（RQ/Redis）。

Submodules:
    jobs.py — worker 端 durable ingestion job 函数。
    task_queue.py — 任务队列抽象：TaskQueue 协议 + FakeQueue（测试）+ RQQueue（RQ/Redis）。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
