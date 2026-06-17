"""FastAPI dependency providers：从 app.state 读取由 create_app 装配好的实例。

均可被 app.dependency_overrides 覆盖，便于测试注入。
"""

from fastapi import Request

from ragspine.agent.llm_provider import LLMProvider
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import TaskQueue


def get_config(request: Request) -> ServiceConfig:
    return request.app.state.config


def get_provider(request: Request) -> LLMProvider:
    return request.app.state.provider


def get_faq_cache(request: Request) -> FAQCache:
    return request.app.state.faq_cache


def get_queue(request: Request) -> TaskQueue:
    return request.app.state.queue
