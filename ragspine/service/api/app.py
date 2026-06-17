"""FastAPI app factory：装配 config/provider/queue/faq_cache 到 app.state。

测试可注入临时 config + mock provider + fake queue + fake FAQ cache；
生产由 from_env + 默认实例装配。HTTP 层只做边界适配，不重写业务 workflow。
"""

from fastapi import FastAPI

from ragspine.agent.llm_provider import LLMProvider
from ragspine.service.api.routes import router
from ragspine.service.config import ServiceConfig, build_provider
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import RQQueue, TaskQueue


def create_app(
    config: ServiceConfig | None = None,
    *,
    provider: LLMProvider | None = None,
    queue: TaskQueue | None = None,
    faq_cache: FAQCache | None = None,
) -> FastAPI:
    config = config or ServiceConfig.from_env()
    provider = provider or build_provider(config)
    if faq_cache is None:
        faq_cache = (
            FAQCache.from_file(config.faq_source)
            if config.faq_source else FAQCache.empty()
        )
    queue = queue or RQQueue(config.redis_url)

    app = FastAPI(title="RAGSpine Service")
    app.state.config = config
    app.state.provider = provider
    app.state.queue = queue
    app.state.faq_cache = faq_cache
    app.include_router(router)
    return app
