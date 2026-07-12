"""FastAPI app factory：装配 config/provider/queue/faq_cache 到 app.state。

测试可注入临时 config + mock provider + fake queue + fake FAQ cache；
生产由 from_env + 默认实例装配。HTTP 层只做边界适配，不重写业务 workflow。
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

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
    # Studio 前端静态站点（可选）：studio_dir 非空且目录存在才挂载，否则静默不挂——
    # 诚实边界：产物是否就位由部署层保证，缺失时 /studio 即 404，API 不受影响。
    if config.studio_dir and Path(config.studio_dir).is_dir():
        app.mount(
            "/studio", StaticFiles(directory=config.studio_dir, html=True), name="studio"
        )
    return app
