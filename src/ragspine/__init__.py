"""RAGSpine —— 无框架的后端 RAG 引擎，纯 Python 装配，反幻觉与来源溯源为代码级不变量。

确定性双通道（结构化数值 + 叙事 RAG）+ agent 编排；所有外部依赖均为 Protocol，
核心零 SDK、可离线运行。代码按领域分组为 10 个顶层域。

Submodules:
    agent/ — agent 编排：意图解析、安全门、tool-use 循环、LLM provider 抽象。
    cli/ — 控制台 CLI 入口（ragspine 命令）：quickstart / ask / version 子命令。
    common/ — 跨域基础原语：company profile、敏感度、glossary、可观测性。
    dify/ — Dify 工作流 YAML → 纯 Python 编译器 + 静态优化建议器（parse/IR/codegen/optimize）。
    eval/ — Q&A 与抽取评测 harness，含基线回归门禁。
    extraction/ — 文档 → 冻结的 StyledGrid IR：抽取器、PDF 分诊、颜色语义、双通道校验。
    fixtures/ — 合成 fixture / ground-truth 生成器（确定性、硬编码），tests 与 scripts 共用。
    graph/ — GraphRAG 域（W7）：确定性结构关系图 + GraphStore 缝 + 叙事 GraphRAG 骨架。
    ingestion/ — IR/文本 → 各类存储：结构化事实、叙事块、人工复核队列。
    n8n/ — n8n workflow JSON ↔ Dify DSL 双向无损转换器（parse/convert 两段）。
    pipeline/ — 管线拓扑导出：从真实装配派生静态 PipelineGraph（Mermaid/DOT/JSON）。
    retrieval/ — 叙事 RAG：切块、BM25 词法、向量、listwise 精排、agent 接入。
    service/ — HTTP 服务层：ServiceConfig、FastAPI app、任务队列、FAQ 短路缓存。
    storage/ — sqlite 存储层：数值事实表（fact_metric），全程保留来源 lineage。
    workflows/ — 自然语言工作流脚手架：模板目录、语义匹配、Dify DSL 生成与格式转换。
"""

# 仅 stdlib，且【不】触发任何第一方 import —— 放在 beartype claw 钩子之前是安全的
# （钩子只需保证第一方子模块在它【之后】才被 import；惰性子模块访问恰好满足）。
import importlib
import pkgutil
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError as _PkgNotFound
from importlib.metadata import version as _pkg_version
from types import ModuleType

# 运行时类型契约（ADR 0004 质量门）：装了 beartype 就在【调用期】对整个包强制每条注解，
# 与 mypy --strict（静态半边）互补。守护式 import：未装时精简离线核心照常 import。
try:
    from beartype import BeartypeConf
    from beartype.claw import beartype_this_package
except ImportError:  # 未装 beartype 时跳过运行时契约
    pass
else:
    # is_pep484_tower=True：采用 PEP 484 隐式数值塔（float 注解亦接受 int、complex 接受
    # float/int），与 mypy / Python 约定一致——否则 beartype 会把 rrf_fuse(k=60) 这类
    # int-传-float 误判为违规。这是对齐标准语义，不是放宽。
    beartype_this_package(conf=BeartypeConf(is_pep484_tower=True))


# 惰性子模块访问工厂（PEP 562），供本包及每个子包的 __init__ 复用：
#
#     from ragspine import _lazy_submodules
#     __getattr__, __dir__ = _lazy_submodules(__name__, __path__)
#
# 效果：`import ragspine` 后可一路 `ragspine.storage.fact_store.FactStore` 点到底，而【每一层】
# 都仅在首次访问时才 import 该子模块 —— 顶层 import 永不急切加载任何域，缺可选 extra（如
# [service]）也不影响 `import ragspine`，只有真正用到的那条链才会拉起对应依赖。子模块在被访问
# 时才 import，此时已在上面的 beartype claw 钩子之后，运行时契约照常覆盖。
def _lazy_submodules(
    pkg_name: str, pkg_path: list[str]
) -> tuple[Callable[[str], ModuleType], Callable[[], list[str]]]:
    def __getattr__(name: str) -> ModuleType:
        if not name.startswith("_"):
            try:
                return importlib.import_module(f"{pkg_name}.{name}")
            except ModuleNotFoundError as exc:
                # 子模块确实不存在（拼写错）→ AttributeError（PEP 562 约定）；
                # 子模块存在但缺第三方可选依赖（如 service 缺 fastapi）→ 原样大声上抛，绝不静默吞。
                if exc.name != f"{pkg_name}.{name}":
                    raise
        raise AttributeError(f"module {pkg_name!r} has no attribute {name!r}")

    def __dir__() -> list[str]:
        return sorted(info.name for info in pkgutil.iter_modules(pkg_path))

    return __getattr__, __dir__


_submodule_getattr, _submodule_dir = _lazy_submodules(__name__, __path__)

# 上手宪法（ADR 0012 rule 3）：包根仅 curated 暴露这 4 个「最小可用 API」名字。
# 仍走惰性解析——`import ragspine` 不急切 import 任何子模块，首次访问某名字时才拉起其源模块。
_CURATED: dict[str, tuple[str, str]] = {
    "FactStore": ("storage.fact_store", "FactStore"),
    "SqliteFactStore": ("storage.fact_store", "SqliteFactStore"),
    "Fact": ("storage.fact_store", "Fact"),
    "MockProvider": ("agent.llm_provider", "MockProvider"),
    "answer_question": ("agent.agent", "answer_question"),
}

__all__ = ("FactStore", "Fact", "MockProvider", "answer_question")

# 包版本(对齐 corespine / spineagent;PyPI 分发名 rag-spine,源码/未装时回落)。
# module 级赋值 → ragspine.__version__ 直接命中 __dict__,不走下面的惰性 __getattr__。
try:
    __version__ = _pkg_version("rag-spine")
except _PkgNotFound:  # 纯源码 / 未安装(无包元数据)场景
    __version__ = "0.0.0+unknown"


def __getattr__(name: str) -> object:
    target = _CURATED.get(name)
    if target is not None:
        module = importlib.import_module(f"{__name__}.{target[0]}")
        symbol: object = getattr(module, target[1])
        return symbol
    return _submodule_getattr(name)


def __dir__() -> list[str]:
    return sorted({*__all__, *_submodule_dir()})
