"""RAGSpine —— 无框架的后端 RAG 引擎，纯 Python 装配，反幻觉与来源溯源为代码级不变量。

确定性双通道（结构化数值 + 叙事 RAG）+ agent 编排；所有外部依赖均为 Protocol，
核心零 SDK、可离线运行。代码按领域分组为 8 个顶层域。

Submodules:
    agent/ — agent 编排：意图解析、安全门、tool-use 循环、LLM provider 抽象。
    common/ — 跨域基础原语：company profile、敏感度、glossary、可观测性。
    eval/ — Q&A 与抽取评测 harness，含基线回归门禁。
    extraction/ — 文档 → 冻结的 StyledGrid IR：抽取器、PDF 分诊、颜色语义、双通道校验。
    ingestion/ — IR/文本 → 各类存储：结构化事实、叙事块、人工复核队列。
    retrieval/ — 叙事 RAG：切块、BM25 词法、向量、listwise 精排、agent 接入。
    service/ — HTTP 服务层：ServiceConfig、FastAPI app、任务队列、FAQ 短路缓存。
    storage/ — sqlite 存储层：数值事实表（fact_metric），全程保留来源 lineage。
"""

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

