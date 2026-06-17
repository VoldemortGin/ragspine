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
