"""三个拓扑构建器：从【真实装配】派生 PipelineGraph（反映 this pipeline 的实际接线）。

设计为叶子级、零编排依赖：本模块【不】在模块层 import 任何 agent / retrieval / service
编排器——一切内省都靠 duck-typing（getattr），所有 Node.symbol 均为 dotted-path 字符串
（仅由漂移守护测试用 importlib 解析）。从而保证零循环 import（screaming-architecture）。

确定性：节点/边一律按声明顺序构造，故同一装配下导出逐字节稳定。

与 PRD 的现实校正（reconciliation）：
- HybridRetriever 不含精排（rerank）——其 search 流程是 预过滤 -> BM25[+向量][×multi-query]
  -> RRF -> top_k。listwise 二审（listwise_rerank）是下游 link 层。故 retriever_topology
  止于 RRF/top_k（无 rerank 节点）；rerank 阶段归入 agent_topology 的叙事分支。
"""

from ragspine.pipeline.graph import Edge, Node, PipelineGraph

# 真实接线的 dotted symbols（漂移守护测试逐个 importlib 解析；概念节点 symbol=None）。
_SYM_PARSE_INTENT = "ragspine.agent.intent.parse_intent"
_SYM_CLARIFY_SCOPE = "ragspine.agent.intent.clarify_scope"
_SYM_SECURITY_GATE = "ragspine.agent.security_gate.SecurityGate"
_SYM_EXECUTE_QUERY_METRIC = "ragspine.agent.query_tools.execute_query_metric"
_SYM_FACT_STORE = "ragspine.storage.fact_store.FactStore"
_SYM_STRUCTURED_ANSWER = "ragspine.agent.agent._structured_answer"
_SYM_RUN_NARRATIVE = "ragspine.agent.agent._run_narrative"
_SYM_ANSWER_QUESTION = "ragspine.agent.agent.answer_question"
_SYM_RUN_TOOL_LOOP = "ragspine.agent.agent._run_tool_loop"
_SYM_BM25 = "ragspine.retrieval.lexical.retrieval.bm25_scores"
_SYM_RRF = "ragspine.retrieval.lexical.retrieval.rrf_fuse"
_SYM_LISTWISE_RERANK = "ragspine.retrieval.rerank.listwise_rerank.listwise_rerank"
_SYM_FAQ_CACHE = "ragspine.service.faq.faq_cache.FAQCache"
_SYM_TASK_QUEUE = "ragspine.service.tasks.task_queue.TaskQueue"


def agent_topology(*, narrative_retriever: object | None = None) -> PipelineGraph:
    """完整请求流的拓扑：parse_intent -> clarify_scope（内部 consults SecurityGate）-> 路由 -> 分支。

    澄清/安全决策点是 clarify_scope（它在内部调用 SecurityGate.screen，见 intent.clarify_scope）——
    SecurityGate 作为被调用依赖（data 边），clarify_scope 返回的四种 ClarificationResult mode 是
    四个出口（条件边，全部从 clarify 出，忠实于"clarify_scope 是返回 mode 的函数"）：
    - out_of_scope -> refuse（竞品/越权最前置拒答）
    - ask_first -> ask（缺指标等歧义先反问）
    - none / answer_with_assumptions -> route（干净问句 / 带假设先答，均继续路由）
    路由菱形（symbol=None 概念节点）按 route 分支：
    - structured：tool loop（_run_tool_loop）-> execute_query_metric -> FactStore -> _structured_answer（反幻觉守护）
    - narrative：narrative_retriever（其 retrieve 内部含 listwise 精排，data 依赖）-> synthesize
    - composite：结构化 + 叙事

    narrative_retriever 缺省（None）时，叙事分支节点【不】出现——拓扑如实反映 this 装配。
    """
    nodes: list[Node] = [
        Node(id="parse", label="parse_intent", kind="stage", domain="agent",
             symbol=_SYM_PARSE_INTENT),
        Node(id="clarify", label="clarify_scope", kind="gate", domain="agent",
             symbol=_SYM_CLARIFY_SCOPE),
        Node(id="gate", label="SecurityGate", kind="gate", domain="agent",
             symbol=_SYM_SECURITY_GATE),
        Node(id="refuse", label="拒答（越权/竞品）", kind="stage", domain="agent"),
        Node(id="ask", label="反问澄清", kind="stage", domain="agent"),
        Node(id="route", label="route?", kind="gate", domain="agent"),
        # structured 分支
        Node(id="tool_loop", label="query_metric tool loop", kind="stage", domain="agent",
             symbol=_SYM_RUN_TOOL_LOOP),
        Node(id="exec_metric", label="execute_query_metric", kind="stage", domain="agent",
             symbol=_SYM_EXECUTE_QUERY_METRIC),
        Node(id="fact_store", label="FactStore", kind="store", domain="storage",
             symbol=_SYM_FACT_STORE),
        Node(id="structured_answer", label="_structured_answer（反幻觉守护）",
             kind="stage", domain="agent", symbol=_SYM_STRUCTURED_ANSWER),
    ]
    edges: list[Edge] = [
        Edge(src="parse", dst="clarify"),
        # clarify_scope 在内部 consults SecurityGate（data 依赖，非顺序流）。
        Edge(src="clarify", dst="gate", label="screens via", kind="data"),
        # clarify_scope 返回的四种 mode 出口（条件边，皆从 clarify 出）。
        Edge(src="clarify", dst="refuse", label="out_of_scope", kind="conditional"),
        Edge(src="clarify", dst="ask", label="ask_first", kind="conditional"),
        Edge(src="clarify", dst="route", label="none", kind="conditional"),
        Edge(src="clarify", dst="route", label="answer_with_assumptions", kind="conditional"),
        # structured 分支
        Edge(src="route", dst="tool_loop", label="route=structured", kind="conditional"),
        Edge(src="tool_loop", dst="exec_metric"),
        Edge(src="exec_metric", dst="fact_store", kind="data"),
        Edge(src="exec_metric", dst="structured_answer"),
    ]

    # 叙事分支：仅当 narrative_retriever 注入时出现（拓扑如实反映 this 装配）。
    if narrative_retriever is not None:
        nodes.extend([
            Node(id="retrieve", label="narrative_retriever（hybrid + 精排）",
                 kind="channel", domain="retrieval"),
            Node(id="rerank", label="listwise rerank", kind="stage", domain="retrieval",
                 symbol=_SYM_LISTWISE_RERANK),
            Node(id="narrative_answer", label="_run_narrative synthesize",
                 kind="stage", domain="agent", symbol=_SYM_RUN_NARRATIVE),
        ])
        edges.extend([
            Edge(src="route", dst="retrieve", label="route=narrative", kind="conditional"),
            Edge(src="route", dst="tool_loop", label="route=composite", kind="conditional"),
            Edge(src="route", dst="retrieve", label="route=composite", kind="conditional"),
            # listwise 精排在 narrative_retriever.retrieve 内部（data 依赖，judge 条件化）；
            # 顺序流直接 retrieve -> synthesize（_run_narrative 调 retrieve 后即 synthesize）。
            Edge(src="retrieve", dst="rerank", label="listwise rerank", kind="data"),
            Edge(src="retrieve", dst="narrative_answer"),
        ])

    return PipelineGraph(
        title="Agent request flow",
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


def retriever_topology(retriever: object) -> PipelineGraph:
    """检索子管线拓扑：预过滤 -> BM25 [+向量] [+multi-query] -> RRF -> top_k。

    duck-typed on retriever.embedding_backend / retriever.query_rewriter（不 import
    HybridRetriever，保持 pipeline 不依赖 orchestrators）：
    - 向量节点仅当 getattr(retriever,'embedding_backend',None) is not None 时出现；
    - multi-query 节点仅当 getattr(retriever,'query_rewriter',None) is not None 时出现。
    无 rerank 节点（精排是下游 link 层，见模块 docstring 的现实校正）。
    """
    has_vector = getattr(retriever, "embedding_backend", None) is not None
    has_multi_query = getattr(retriever, "query_rewriter", None) is not None

    nodes: list[Node] = [
        Node(id="prefilter", label="元数据预过滤", kind="stage", domain="retrieval"),
    ]
    edges: list[Edge] = []

    # multi-query 改写在打分【之前】把 query 扩写成多个变体（search 对每个变体跑 BM25/向量）；
    # 故打分通道的上游是 multi_query（若注入）否则 prefilter——multi_query 不再是悬空汇点。
    scoring_src = "prefilter"
    if has_multi_query:
        nodes.append(
            Node(id="multi_query", label="multi-query 改写", kind="stage", domain="retrieval")
        )
        edges.append(Edge(src="prefilter", dst="multi_query", kind="data"))
        scoring_src = "multi_query"

    nodes.append(Node(id="bm25", label="BM25", kind="stage", domain="retrieval", symbol=_SYM_BM25))
    edges.append(Edge(src=scoring_src, dst="bm25"))
    if has_vector:
        nodes.append(
            Node(id="vector", label="向量通道", kind="channel", domain="retrieval")
        )
        edges.append(Edge(src=scoring_src, dst="vector"))

    nodes.extend([
        Node(id="rrf", label="RRF 融合", kind="stage", domain="retrieval", symbol=_SYM_RRF),
        Node(id="top_k", label="top_k", kind="stage", domain="retrieval"),
    ])
    edges.append(Edge(src="bm25", dst="rrf"))
    if has_vector:
        edges.append(Edge(src="vector", dst="rrf"))
    edges.append(Edge(src="rrf", dst="top_k"))

    return PipelineGraph(
        title="HybridRetriever sub-pipeline",
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


def service_topology(app: object) -> PipelineGraph:
    """服务层拓扑：FAQ 短路在 agent 上游 + 异步 ingestion 路径。

    duck-typed on app.state.faq_cache / app.state.queue（不 import FastAPI app 构造器）：
    - ask 流：request -> FAQ {hit -> answer | miss -> agent}（FAQ 节点在 agent 上游）；
    - 异步 ingestion 路径：routes -> queue -> jobs。
    """
    state = getattr(app, "state", None)
    has_faq = getattr(state, "faq_cache", None) is not None
    has_queue = getattr(state, "queue", None) is not None

    nodes: list[Node] = [Node(id="request", label="HTTP /v1/ask", kind="external", domain="service")]
    edges: list[Edge] = []

    if has_faq:
        nodes.append(
            Node(id="faq", label="FAQ 短路", kind="gate", domain="service", symbol=_SYM_FAQ_CACHE)
        )
        edges.append(Edge(src="request", dst="faq"))
        nodes.append(Node(id="faq_answer", label="FAQ 命中回答", kind="stage", domain="service"))
        edges.append(Edge(src="faq", dst="faq_answer", label="hit", kind="conditional"))
        agent_src, agent_label, agent_kind = "faq", "miss", "conditional"
    else:
        agent_src, agent_label, agent_kind = "request", None, "flow"

    # agent 在 FAQ 下游（FAQ 短路 upstream of agent）。
    nodes.append(
        Node(id="agent", label="answer_question", kind="stage", domain="agent",
             symbol=_SYM_ANSWER_QUESTION)
    )
    edges.append(Edge(src=agent_src, dst="agent", label=agent_label, kind=agent_kind))

    # 异步 ingestion 路径：routes -> queue -> jobs。
    if has_queue:
        nodes.extend([
            Node(id="ingest_routes", label="ingestion routes", kind="external", domain="service"),
            Node(id="queue", label="TaskQueue", kind="store", domain="service",
                 symbol=_SYM_TASK_QUEUE),
            Node(id="jobs", label="ingestion jobs", kind="stage", domain="service"),
        ])
        edges.extend([
            Edge(src="ingest_routes", dst="queue"),
            Edge(src="queue", dst="jobs"),
        ])

    return PipelineGraph(
        title="Service topology",
        nodes=tuple(nodes),
        edges=tuple(edges),
    )
