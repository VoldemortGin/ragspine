"""FastAPI dependency providers：从 app.state 读取由 create_app 装配好的实例。

均可被 app.dependency_overrides 覆盖，便于测试注入。
"""

from typing import cast

from fastapi import Request

from ragspine.agent.llm_provider import LLMProvider
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.studio.launch import LaunchSessionRegistry
from ragspine.service.tasks.task_queue import TaskQueue
from ragspine.workflows.matching import TemplateMatcher


def get_config(request: Request) -> ServiceConfig:
    return cast(ServiceConfig, request.app.state.config)


def get_provider(request: Request) -> LLMProvider:
    return cast(LLMProvider, request.app.state.provider)


def get_faq_cache(request: Request) -> FAQCache:
    return cast(FAQCache, request.app.state.faq_cache)


def get_queue(request: Request) -> TaskQueue:
    return cast(TaskQueue, request.app.state.queue)


def get_workflow_matcher(request: Request) -> TemplateMatcher:
    return cast(TemplateMatcher, request.app.state.workflow_matcher)


def get_launch_sessions(request: Request) -> LaunchSessionRegistry:
    return cast(LaunchSessionRegistry, request.app.state.launch_sessions)
