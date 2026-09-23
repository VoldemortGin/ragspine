"""common.evidence —— 证据链(enterprise-pdf-rag 产品线)的跨切面配置、日志与模型访问(ADR 0022)。

APP_* 配置叶子与 ragspine 的 RAGSpineConfig / ServiceConfig 是两套,故意不合并;血缘日志不属于
observability(后者有隐私不变量)。

Submodules:
    logging.py — 日志配置的唯一来源 + AI 产物的血缘与隐私纪律。
    providers/ — 显式 provider 环境、有界 JSON 模型调用与缓存、本地 embedding/rerank 适配与回环隧道。
    settings.py — APP_* 配置、项目根锚点、运行期目录与包内资源定位的唯一来源。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
