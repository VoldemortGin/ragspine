"""拓扑构建器（agent / retriever / service）的行为测试（TDD，Phase 2）。

只验证对外行为：三个构建器在【真实装配】下产出的 PipelineGraph 形态——
分支/门齐全、条件边带标签、接线如实（向量/multi-query/叙事按是否注入出现）、
FAQ 在 agent 上游、异步 routes->queue->jobs、漂移守护（每个非 None symbol 可 importlib
解析）、以及确定性（同装配导出两次逐字节一致）。

覆盖 PRD 测试类目 2（agent 三分支 + 澄清/拒答门 + 带标签条件边）、3（接线如实）、
4（FAQ 上游 + 异步路径）、6（漂移守护）、8（determinism）。

每个用例 docstring 带中文 user story。
"""

import importlib
import os

import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from ragspine.agent.llm_provider import MockProvider
from ragspine.pipeline.topology import agent_topology, service_topology
from ragspine.retrieval.lexical.retrieval import GlossaryQueryRewriter, HybridRetriever
from ragspine.service.api.app import create_app
from ragspine.service.config import ServiceConfig
from ragspine.service.faq.faq_cache import FAQCache
from ragspine.service.tasks.task_queue import FakeQueue


# --- 测试替身 ---------------------------------------------------------------
class _FakeEmbedding:
    """确定性 fake embedding backend（仅用于触发向量节点）。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] for t in texts]


class _State:
    def __init__(self, *, faq_cache: object | None, queue: object | None) -> None:
        self.faq_cache = faq_cache
        self.queue = queue


class _FakeApp:
    def __init__(self, *, faq_cache: object | None, queue: object | None) -> None:
        self.state = _State(faq_cache=faq_cache, queue=queue)


def _resolve_symbol(symbol: str) -> object:
    """漂移守护 helper：把 dotted symbol 拆成 module + attr，importlib 解析。

    解析失败（模块不存在 / attr 缺失）抛出 ImportError/AttributeError。
    """
    module_path, _, attr = symbol.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def _all_symbols(graph: object) -> list[str]:
    return [n.symbol for n in graph.nodes if n.symbol is not None]


# --- 类目 2：agent 三分支 + 门 + 带标签条件边 ------------------------------
def test_agent_topology_has_three_route_branches_and_gates():
    """作为调试路由的开发者，我要 agent 拓扑含三路分支 + 澄清/拒答门，且条件边带标签。"""
    g = agent_topology(narrative_retriever=object())

    node_ids = {n.id for n in g.nodes}
    # 三路分支的代表节点齐全。
    assert {"tool_loop", "structured_answer"} <= node_ids  # structured
    assert {"retrieve", "narrative_answer"} <= node_ids  # narrative（含 composite 共用）
    # 澄清/拒答门齐全。
    assert {"refuse", "ask"} <= node_ids

    # 路由是菱形门（gate）概念节点（symbol=None）。
    route = next(n for n in g.nodes if n.id == "route")
    assert route.kind == "gate"
    assert route.symbol is None

    # 条件边带标签：三种 route 标签 + 两个门标签都在。
    labels = {e.label for e in g.edges}
    assert {"route=structured", "route=narrative", "route=composite"} <= labels
    assert {"out_of_scope", "ask_first", "answer_with_assumptions"} <= labels

    # 路由分支边为 conditional 类型。
    route_edges = [e for e in g.edges if e.src == "route"]
    assert route_edges  # 非空
    assert all(e.kind == "conditional" for e in route_edges)


def test_agent_topology_clarify_emits_four_modes_and_consults_gate():
    """作为开发者，我要四种 ClarificationResult mode 的出口都从 clarify_scope 出
    （out_of_scope->拒答、ask_first->反问、none/answer_with_assumptions->路由），
    且 SecurityGate 是 clarify_scope 内部调用的依赖（data 边）——忠实于真实接线
    （intent.clarify_scope 在内部 screen，answer_question 按其返回的 mode 分流）。"""
    g = agent_topology()
    clarify_edges = {(e.src, e.dst, e.label) for e in g.edges if e.src == "clarify"
                     and e.kind == "conditional"}
    assert ("clarify", "refuse", "out_of_scope") in clarify_edges
    assert ("clarify", "ask", "ask_first") in clarify_edges
    assert ("clarify", "route", "answer_with_assumptions") in clarify_edges
    assert ("clarify", "route", "none") in clarify_edges  # CLARIFY_NONE：干净问句直达路由
    # 没有出口边错误地从 gate 出（gate 是被 clarify 调用的依赖，不是顺序门）。
    assert not any(e.src == "gate" for e in g.edges)
    gate_dep = next(e for e in g.edges if e.dst == "gate")
    assert (gate_dep.src, gate_dep.kind) == ("clarify", "data")


# --- 类目 3：接线如实 --------------------------------------------------------
def test_agent_topology_narrative_branch_absent_when_no_retriever():
    """作为开发者，我要叙事检索缺省时叙事分支节点【不】出现（拓扑如实反映装配）。"""
    g = agent_topology()  # narrative_retriever 缺省
    node_ids = {n.id for n in g.nodes}
    assert "retrieve" not in node_ids
    assert "narrative_answer" not in node_ids
    assert "rerank" not in node_ids
    # 结构化分支仍在。
    assert {"tool_loop", "structured_answer"} <= node_ids
    # 不应有 narrative/composite 路由标签。
    labels = {e.label for e in g.edges}
    assert "route=narrative" not in labels
    assert "route=composite" not in labels


def test_agent_topology_narrative_branch_present_when_retriever_wired():
    """作为开发者，我要叙事检索接入时叙事分支（检索->rerank->synthesize）出现。"""
    g = agent_topology(narrative_retriever=object())
    node_ids = {n.id for n in g.nodes}
    assert {"retrieve", "rerank", "narrative_answer"} <= node_ids
    # rerank 在叙事分支里（PRD-vs-reality 校正：rerank 是下游层，不在 retriever 子图）。
    rerank = next(n for n in g.nodes if n.id == "rerank")
    assert rerank.symbol == "ragspine.retrieval.rerank.listwise_rerank.listwise_rerank"


def test_retriever_topology_pure_bm25_has_no_vector_no_multi_query():
    """作为用户，我要纯 BM25 装配时拓扑里无向量、无 multi-query 节点（如实反映）。"""
    retriever = HybridRetriever([])  # 无 embedding_backend / query_rewriter
    g = retriever.topology()
    node_ids = {n.id for n in g.nodes}
    assert "vector" not in node_ids
    assert "multi_query" not in node_ids
    # 基本骨架在：预过滤 -> BM25 -> RRF -> top_k；且无 rerank。
    assert {"prefilter", "bm25", "rrf", "top_k"} <= node_ids
    assert "rerank" not in node_ids


def test_retriever_topology_vector_node_only_when_embedding_present():
    """作为用户，我要注入 embedding backend 后向量节点出现，且接入 RRF。"""
    retriever = HybridRetriever([], embedding_backend=_FakeEmbedding())
    g = retriever.topology()
    node_ids = {n.id for n in g.nodes}
    assert "vector" in node_ids
    assert "multi_query" not in node_ids
    # 向量节点参与 RRF 融合。
    assert ("vector", "rrf") in {(e.src, e.dst) for e in g.edges}


def test_retriever_topology_vector_node_names_resolved_store():
    """作为用户，我要 .topology() 标注向量缝解析到的具体 store（label 含类名 + symbol 可解析）。

    has_vector 时检索器的 vector_store 已解析为具体实现（默认 InProcessVectorStore）；
    向量节点据此命名，让拓扑如实回答「这条管线用的是哪个向量后端」。
    """
    retriever = HybridRetriever([], embedding_backend=_FakeEmbedding())
    g = retriever.topology()
    vector = next(n for n in g.nodes if n.id == "vector")
    assert "InProcessVectorStore" in vector.label
    assert vector.symbol == "ragspine.retrieval.vector.store.InProcessVectorStore"
    assert _resolve_symbol(vector.symbol) is not None  # 漂移守护：命名的 store 类可解析


def test_retriever_topology_multi_query_node_only_when_rewriter_present():
    """作为用户，我要注入 query rewriter 后 multi-query 节点出现。"""
    retriever = HybridRetriever([], query_rewriter=GlossaryQueryRewriter())
    g = retriever.topology()
    node_ids = {n.id for n in g.nodes}
    assert "multi_query" in node_ids
    assert "vector" not in node_ids


def test_retriever_topology_both_channels_present():
    """作为用户，我要同时注入两者时向量与 multi-query 都出现。"""
    retriever = HybridRetriever(
        [], embedding_backend=_FakeEmbedding(), query_rewriter=GlossaryQueryRewriter()
    )
    g = retriever.topology()
    node_ids = {n.id for n in g.nodes}
    assert {"vector", "multi_query"} <= node_ids


def test_retriever_topology_multi_query_feeds_scoring_not_dangling():
    """作为用户，我要 multi-query 改写接入打分通道（prefilter->multi_query->BM25/向量），
    而非悬空汇点——忠实于 search 对每个改写变体跑 BM25/向量打分。"""
    retriever = HybridRetriever(
        [], embedding_backend=_FakeEmbedding(), query_rewriter=GlossaryQueryRewriter()
    )
    g = retriever.topology()
    edges = {(e.src, e.dst) for e in g.edges}
    assert ("prefilter", "multi_query") in edges
    assert ("multi_query", "bm25") in edges    # 扩写后对每个变体跑 BM25
    assert ("multi_query", "vector") in edges  # 也跑向量
    # multi_query 不再是悬空汇点（必有出边）。
    assert any(e.src == "multi_query" for e in g.edges)


# --- 类目 4：服务拓扑 FAQ 上游 + 异步路径 ----------------------------------
def test_service_topology_faq_upstream_of_agent():
    """作为装配服务的用户，我要服务拓扑里 FAQ 短路位于 agent 上游（miss 才到 agent）。"""
    app = _FakeApp(faq_cache=object(), queue=object())
    g = service_topology(app)
    node_ids = {n.id for n in g.nodes}
    assert {"request", "faq", "agent"} <= node_ids
    edges = {(e.src, e.dst, e.label) for e in g.edges}
    # request -> FAQ -> {hit -> faq_answer | miss -> agent}
    assert ("request", "faq", None) in edges
    assert ("faq", "faq_answer", "hit") in edges
    assert ("faq", "agent", "miss") in edges


def test_service_topology_async_path_routes_queue_jobs():
    """作为用户，我要看到异步 ingestion 路径：routes -> queue -> jobs。"""
    app = _FakeApp(faq_cache=object(), queue=object())
    g = service_topology(app)
    edges = {(e.src, e.dst) for e in g.edges}
    assert ("ingest_routes", "queue") in edges
    assert ("queue", "jobs") in edges


def test_service_topology_on_real_app():
    """作为用户，我要真实 create_app 装配也能导出含 FAQ 上游 + queue 的拓扑。"""
    app = create_app(
        ServiceConfig.from_env(),
        provider=MockProvider(),
        queue=FakeQueue(),
        faq_cache=FAQCache.empty(),
    )
    g = service_topology(app)
    node_ids = {n.id for n in g.nodes}
    assert {"faq", "agent", "queue"} <= node_ids


# --- 类目 6：漂移守护 --------------------------------------------------------
def test_drift_guard_all_shipped_symbols_resolve():
    """作为维护者，我要每个 shipped 拓扑的非 None Node.symbol 都能 importlib 解析。"""
    retriever = HybridRetriever(
        [], embedding_backend=_FakeEmbedding(), query_rewriter=GlossaryQueryRewriter()
    )
    app = create_app(
        ServiceConfig.from_env(),
        provider=MockProvider(),
        queue=FakeQueue(),
        faq_cache=FAQCache.empty(),
    )
    graphs = [
        agent_topology(narrative_retriever=object()),
        retriever.topology(),
        service_topology(app),
    ]
    symbols = {s for g in graphs for s in _all_symbols(g)}
    assert symbols  # 至少有一些 code-backed 节点
    for symbol in symbols:
        # 解析失败会抛异常 -> 用例 FAIL（即漂移被拦截）。
        assert _resolve_symbol(symbol) is not None


def test_drift_guard_all_declared_constants_resolve():
    """作为维护者，我要 topology 模块里【声明的】每个 _SYM_* 常量都能解析——权威覆盖，
    即便某符号只挂在未被本测试触发的分支上，也不会漏过（不止 walk 已构建的 graph）。"""
    import ragspine.pipeline.topology as topo

    declared = {v for k, v in vars(topo).items()
                if k.startswith("_SYM_") and isinstance(v, str)}
    assert declared  # 确有声明的符号
    for symbol in declared:
        assert _resolve_symbol(symbol) is not None


def test_drift_guard_rejects_bogus_and_renamed_symbols():
    """作为维护者，我要漂移守护拦截两类漂移：模块消失（ImportError）与真实模块上
    函数被重命名/删除（AttributeError，最常见的重构漂移），且 bogus symbol 一旦挂进
    真实 graph 节点，同一守护循环必须抛错。"""
    import pytest

    from ragspine.pipeline.graph import Node, PipelineGraph

    with pytest.raises((ImportError, AttributeError)):
        _resolve_symbol("ragspine.nope.gone")  # 模块不存在
    with pytest.raises(AttributeError):
        _resolve_symbol("ragspine.agent.agent._not_a_real_function")  # 真实模块、attr 缺失
    # bogus symbol 挂进真实 graph，走 _all_symbols + _resolve_symbol 守护循环必被拦。
    bogus = PipelineGraph(
        title="x",
        nodes=(Node(id="n", label="n", kind="stage", symbol="ragspine.agent.agent._gone"),),
        edges=(),
    )
    with pytest.raises((ImportError, AttributeError)):
        for symbol in _all_symbols(bogus):
            _resolve_symbol(symbol)


# --- 类目 8：确定性 ----------------------------------------------------------
def test_agent_topology_export_is_byte_identical_across_calls():
    """作为贡献者，我要 agent_topology().to_mermaid() 两次调用逐字节一致。"""
    assert (
        agent_topology(narrative_retriever=object()).to_mermaid()
        == agent_topology(narrative_retriever=object()).to_mermaid()
    )


def test_retriever_topology_export_is_byte_identical_across_calls():
    """作为贡献者，我要 retriever 拓扑导出确定（同装配两次一致）。"""
    retriever = HybridRetriever([], embedding_backend=_FakeEmbedding())
    assert retriever.topology().to_mermaid() == retriever.topology().to_mermaid()
    assert retriever.topology().to_dot() == retriever.topology().to_dot()


def test_service_topology_export_is_byte_identical_across_calls():
    """作为贡献者，我要 service 拓扑导出确定（同 app 两次一致）。"""
    app = _FakeApp(faq_cache=object(), queue=object())
    assert service_topology(app).to_mermaid() == service_topology(app).to_mermaid()


def test_topology_export_deterministic_across_processes():
    """作为贡献者，我要导出【跨进程】也逐字节一致——单进程内 set/dict 迭代序稳定，catch 不到
    哈希序乱序；用不同 PYTHONHASHSEED 跑两个子进程，证明导出不依赖任何哈希序（真确定）。"""
    import subprocess
    import sys

    code = (
        "from ragspine.pipeline.topology import agent_topology;"
        "print(agent_topology(narrative_retriever=object()).to_mermaid(), end='')"
    )

    def _run(seed: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=str(ROOT_DIR),
        )

    out0, out1 = _run("0"), _run("1")
    assert out0.returncode == 0 and out1.returncode == 0, (out0.stderr, out1.stderr)
    assert out0.stdout  # 非空
    assert out0.stdout == out1.stdout  # 跨进程逐字节一致
