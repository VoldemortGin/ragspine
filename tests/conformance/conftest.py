"""Conformance 测试夹共享夹具：把每条用例参数化到【每一个注册的 VectorStore 实现】。

设计意图（见 src/ragspine/retrieval/docs/vector-store.md 与 docs/prd-breadth-via-adapters.md）：
    一个 Protocol -> 一套共享测试。任何 VectorStore 实现（现在只有内存默认实现
    InProcessVectorStore，将来的 Qdrant / pgvector / FAISS 适配器）只要登记到
    VECTOR_STORE_IMPLS，就【自动继承】全部合约测试与不变量绑定测试——这正是
    「敢放手让社区/第三方填广度、却让脊柱不变量烂不掉」的机制：不通过 conformance
    的 adapter 直接 CI 红，而非生产事故。

红色策略（重要）：对 ragspine.retrieval.vector.store 的 import 一律【延迟到夹具内】，
    使 conftest 与测试模块本身都能干净收集。VectorStore 落地前，每条用例在夹具 setup
    阶段因 ModuleNotFoundError 报 ERROR（红色、逐条隔离），而【不会】中断整轮 `pytest
    tests/`——既有 943 绿色用例照常运行。落地后全部转绿。
"""

import os

import pytest
import rootutils

ROOT_DIR = rootutils.setup_root(os.getcwd(), indicator=".project-root", pythonpath=True)

from corespine import ConformanceSuite, InvariantPack, Registry


# ---------------------------------------------------------------------------
# 注册表：受 conformance 约束的 VectorStore 实现 -> 其【能力等级】（仅登记名字+能力，类延迟解析）。
# 能力等级驱动确定性断言的强弱（见 test_vector_store_invariants.py 的 capability 分支）：
#   - exact       —— 打分精确、可复现，断言【逐位 byte-identical + id 升序破平分】（全量强度）。
#   - approximate —— 生产保证为近似（HNSW 等），只断言较弱的「同实例重复调用顺序稳定 + recall@k 下限」；
#                    provenance / isolation / where 过滤下推三项不变量仍【全量绑定】，绝不松动。
# 第三方实现在此追加 名字->能力 一行 + 在 _resolve_impl 里补一行即继承全套
# （将来可换成 entry-point 自动发现）。
# ---------------------------------------------------------------------------
VECTOR_STORE_IMPLS = {
    "in_process": "exact",
    "sqlite_vec": "exact",
    "pgvector": "exact",
    "qdrant": "approximate",
}


def _resolve_impl(name: str):
    """名字 -> VectorStore 实现类（延迟 import，红色阶段在此抛 ModuleNotFoundError）。"""
    from ragspine.retrieval.vector.store import InProcessVectorStore

    if name == "in_process":
        return InProcessVectorStore
    if name == "sqlite_vec":
        from ragspine.retrieval.vector.adapters.sqlite_vec import SqliteVecVectorStore

        return SqliteVecVectorStore
    if name == "pgvector":
        from ragspine.retrieval.vector.adapters.pgvector import PgVectorVectorStore

        return PgVectorVectorStore
    if name == "qdrant":
        from ragspine.retrieval.vector.adapters.qdrant import QdrantVectorStore

        return QdrantVectorStore
    raise KeyError(name)


@pytest.fixture(params=list(VECTOR_STORE_IMPLS), ids=list(VECTOR_STORE_IMPLS))
def vector_store(request):
    """每个注册实现各给一个【全新空库】实例；每条用例对所有实现各跑一遍。

    真实后端 behind extra / 需外部服务：缺时该参数 skip（黄，不红），齐备即整套 gate。
        sqlite_vec —— 需 [vector]（嵌入式，装了即跑）。
        pgvector   —— 需 [vector] + 环境变量 RAGSPINE_PG_URL 指向带 pgvector 扩展的 Postgres
                      （服务型后端，默认本地 CI 无 PG 故 skip；配了 URL 即整套 gate，本地实测见 README）。
        qdrant     —— 需 [vector]（local 模式纯进程内、零服务，装了即跑，无需 env 门）。
    """
    if request.param == "sqlite_vec":
        pytest.importorskip("sqlite_vec", reason="sqlite-vec 未装（pip install ragspine[vector]）")
    if request.param == "pgvector":
        pytest.importorskip("pg8000", reason="pg8000 未装（pip install ragspine[vector]）")
        if not os.environ.get("RAGSPINE_PG_URL"):
            pytest.skip("RAGSPINE_PG_URL 未设（pgvector conformance 需带 pgvector 扩展的 Postgres）")
    if request.param == "qdrant":
        pytest.importorskip("qdrant_client", reason="qdrant-client 未装（pip install ragspine[vector]）")
    store = _resolve_impl(request.param)()
    yield store
    close = getattr(store, "close", None)
    if callable(close):
        close()  # sqlite_vec / pgvector / qdrant 收尾关连接（pgvector 的 TEMP 表随之 drop）


@pytest.fixture
def vector_store_capability(request) -> str:
    """当前参数化 VectorStore 实现的能力等级（exact / approximate）——驱动确定性断言强弱分支。

    从注册表按 vector_store 夹具的当前 param 名解析（registry-driven，一行登记即生效）。
    """
    return VECTOR_STORE_IMPLS[request.node.callspec.params["vector_store"]]


# ---------------------------------------------------------------------------
# 注册表：受 conformance 约束的 SourceConnector 实现（名字）。第三方实现在此追加一行 +
# 在 _build_source_connector 里补一行（或将来换成 entry-point 自动发现）即继承整套 provenance pack。
# ---------------------------------------------------------------------------
SOURCE_CONNECTOR_IMPLS = ("filesystem",)


def _build_source_connector(name: str, root):
    """名字 -> 建在 fixture 树根 root 上的 SourceConnector 实例（延迟 import，红色阶段在此抛 ModuleNotFoundError）。"""
    if name == "filesystem":
        from ragspine.ingestion.source.connector import FilesystemConnector

        return FilesystemConnector(root)
    raise KeyError(name)


@pytest.fixture
def source_tree(tmp_path):
    """确定性夹具树：嵌套目录 + 多文件，外加一个隐藏文件与一个 Office 临时文件（后两者应被忽略）。

    可见文件：a.pptx / sub/b.pdf / sub/c.txt（source_doc_id 即文件名 a.pptx / b.pdf / c.txt）。
    """
    (tmp_path / "a.pptx").write_bytes(b"alpha")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.pdf").write_bytes(b"%PDF-bravo")
    (tmp_path / "sub" / "c.txt").write_bytes(b"charlie")
    (tmp_path / ".hidden.pptx").write_bytes(b"nope")  # 隐藏文件，应被忽略
    (tmp_path / "~$temp.pptx").write_bytes(b"nope")   # Office 临时文件，应被忽略
    return tmp_path


@pytest.fixture(params=list(SOURCE_CONNECTOR_IMPLS), ids=list(SOURCE_CONNECTOR_IMPLS))
def source_connector(request, source_tree):
    """每个注册 SourceConnector 各给一个建在 fixture 树上的实例；每条 provenance 用例对所有实现各跑一遍。"""
    return _build_source_connector(request.param, source_tree)


# ---------------------------------------------------------------------------
# 注册表：受 conformance 约束的 Chunker 实现（名字）。第三方实现在此追加一行 +
# 在 _build_chunker 里补一行（或经 make_chunker 的 entry-point 自动发现）即继承整套 provenance pack。
# ---------------------------------------------------------------------------
CHUNKER_IMPLS = ("default", "layout", "sentence_window", "semantic")


def _build_chunker(name: str):
    """名字 -> Chunker 实例（延迟 import，红色阶段在此抛 ModuleNotFoundError）。"""
    if name == "default":
        from ragspine.retrieval.chunking.chunker import DefaultChunker

        return DefaultChunker()
    if name == "layout":
        from ragspine.retrieval.chunking.layout_chunker import LayoutAwareChunker

        return LayoutAwareChunker()
    if name == "sentence_window":
        from ragspine.retrieval.chunking.sentence_window_chunker import (
            SentenceWindowChunker,
        )

        return SentenceWindowChunker()
    if name == "semantic":
        from ragspine.retrieval.chunking.semantic_chunker import SemanticChunker

        return SemanticChunker()
    raise KeyError(name)


@pytest.fixture(params=list(CHUNKER_IMPLS), ids=list(CHUNKER_IMPLS))
def chunker(request):
    """每个注册 Chunker 各给一个实例；每条 provenance 用例对所有实现各跑一遍。"""
    return _build_chunker(request.param)


def _record(record_id: str, vector, **metadata):
    """构造 VectorRecord：默认带齐血缘 + 过滤元数据，可逐项覆盖（延迟 import 以保持惰性红色）。

    默认 metadata 含 doc_id / source_locator（provenance 锚点）与
    topic/entity/geography/period/language/sensitivity（过滤维度），口径同 StoredChunk。
    vector 接受任意可迭代数，统一转 float 元组。供 make_record 夹具与 InvariantPack 共用。
    """
    from ragspine.retrieval.vector.store import VectorRecord

    md = dict(
        doc_id=record_id.split("#")[0],
        source_locator=f"{record_id}!para1",
        topic="FIN",
        entity="ACME_HK",
        geography="HK",
        period="2025H1",
        language="zh",
        sensitivity="INTERNAL",
    )
    md.update({k: str(v) for k, v in metadata.items()})
    return VectorRecord(id=record_id, vector=tuple(float(x) for x in vector), metadata=md)


@pytest.fixture
def make_record():
    """构造 VectorRecord 的工厂夹具：默认带齐血缘 + 过滤元数据，可逐项覆盖。

    薄封装 _record（与下方 InvariantPack 共用同一构造口径）；延迟 import VectorRecord
    以保持惰性红色。
    """
    return _record


# ===========================================================================
# corespine 机制层 conformance 骨架（与上文 fixture-形态参数化两层互补，领域断言不外迁）。
#
# 「哪些实现受机制级不变量约束」这件【机制】交给 corespine：用 corespine.Registry 持有
# 名字->工厂 的单一出处，再用 corespine.ConformanceSuite 把 实现 × InvariantPack 绑成
# 笛卡尔积，由 test_vector_store_suite.py 的 parametrize_kwargs() 两行消费。
#
# 范围刻意收敛在【离线零依赖、零服务】的 in_process 默认实现上——它无 importorskip / env 门，
# 故 corespine 套件每格都能确定性执行；sqlite_vec / pgvector / qdrant 的可用性门与能力分支
# 仍由上文 vector_store / vector_store_capability 夹具承载，二者职责不重叠。
#
# 红色策略：工厂与不变量都【延迟 import】store，未落地时在调用 thunk 时报 ERROR（逐格隔离），
# 绝不中断整轮收集。领域语义（cosine 排序 / where 下推 / 血缘回传）仍全部留在
# test_vector_store_contract.py / test_vector_store_invariants.py，本处只组织机制。
# ===========================================================================
def _make_in_process():
    """构造内存默认实现（延迟 import，红色阶段在此抛 ModuleNotFoundError）。"""
    from ragspine.retrieval.vector.store import InProcessVectorStore

    return InProcessVectorStore()


VECTOR_STORE_REGISTRY: "Registry" = Registry("vector_store")
VECTOR_STORE_REGISTRY.register("in_process", _make_in_process)


def _determinism_repeated_query_stable(store) -> None:
    """同库同查询两次：命中 (id, score) 序列逐位一致（确定性·机制）。"""
    store.upsert([_record(f"d{i}#0", [1.0, float(i) * 0.1, 0.0]) for i in range(8)])
    first = [(h.id, h.score) for h in store.query([1.0, 0.05, 0.0], k=8)]
    second = [(h.id, h.score) for h in store.query([1.0, 0.05, 0.0], k=8)]
    assert first == second


def _provenance_id_and_locator_round_trip(store) -> None:
    """血缘锚点 id / doc_id / source_locator 经 upsert->query 原样回传（不臆造·机制）。"""
    store.upsert([
        _record("HK_FIN.pptx#3", [1.0, 0.0, 0.0],
                doc_id="HK_FIN.pptx", source_locator="HK_FIN.pptx!slide3#para2"),
    ])
    [hit] = store.query([1.0, 0.0, 0.0], k=1)
    assert hit.id == "HK_FIN.pptx#3"
    assert hit.metadata["doc_id"] == "HK_FIN.pptx"
    assert hit.metadata["source_locator"] == "HK_FIN.pptx!slide3#para2"


def _isolation_filter_excludes_nearest(store) -> None:
    """where 下推：被过滤记录即便是最近邻也不出现（敏感度隔离下推·机制）。"""
    store.upsert([
        _record("secret#0", [1.0, 0.0, 0.0], sensitivity="RESTRICTED"),  # 最近邻
        _record("pub#0", [0.0, 1.0, 0.0], sensitivity="INTERNAL"),
    ])
    out_ids = {h.id for h in store.query([1.0, 0.0, 0.0], k=5, where={"sensitivity": "INTERNAL"})}
    assert "secret#0" not in out_ids
    assert "pub#0" in out_ids


VECTOR_STORE_INVARIANTS: "InvariantPack" = (
    InvariantPack("vector_store")
    .add("determinism_repeated_query_stable", _determinism_repeated_query_stable)
    .add("provenance_id_and_locator_round_trip", _provenance_id_and_locator_round_trip)
    .add("isolation_filter_excludes_nearest", _isolation_filter_excludes_nearest)
)

# 实现 × 不变量 的笛卡尔积套件：corespine 负责「跑全套 + 定位坏格子」。
# 工厂取自 Registry（单一出处）；每格新建实例，杜绝实现间状态串味。
VECTOR_STORE_SUITE: "ConformanceSuite" = ConformanceSuite(
    {name: (lambda n=name: VECTOR_STORE_REGISTRY.make(n)) for name in VECTOR_STORE_REGISTRY.names()},
    VECTOR_STORE_INVARIANTS,
)


# ---------------------------------------------------------------------------
# 注册表：受 conformance 约束的 GraphStore 实现（W7c GraphStore 缝）。第三方实现在此追加一行
# + 在 _build_graph_store 里补一行（或将来换成 entry-point 自动发现）即继承整套 provenance /
# isolation / determinism pack——同 VectorStore 缝的范式。in_process 是零依赖确定性默认；
# networkx 是 behind [graph] extra 的薄 adapter（NetworkxGraphStore），由 fixture 的
# importorskip 门控（缺则该参数 skip，黄不红）。
# ---------------------------------------------------------------------------
GRAPH_STORE_IMPLS = ("in_process", "networkx")


def _build_graph_store(name: str):
    """名字 -> GraphStore 实例（延迟 import，红色阶段在此抛 ModuleNotFoundError）。"""
    from ragspine.graph.store import InProcessGraphStore

    if name == "in_process":
        return InProcessGraphStore()
    if name == "networkx":
        from ragspine.graph.adapters.networkx_store import NetworkxGraphStore

        return NetworkxGraphStore()
    raise KeyError(name)


@pytest.fixture(params=list(GRAPH_STORE_IMPLS), ids=list(GRAPH_STORE_IMPLS))
def graph_store(request):
    """每个注册 GraphStore 各给一个【全新空库】实例；每条用例对所有实现各跑一遍。

    networkx behind [graph] extra：缺时该参数 skip（黄，不红），装了即整套 gate（本 venv 已装）。
    """
    if request.param == "networkx":
        pytest.importorskip("networkx", reason="networkx 未装（pip install ragspine[graph]）")
    return _build_graph_store(request.param)


# ===========================================================================
# corespine 机制层 conformance 骨架（GraphStore 缝）——与上文 graph_store fixture-形态参数化
# 两层互补，领域断言不外迁。范围刻意收敛在【离线零依赖、零服务】的 in_process 默认实现上
# （无 importorskip / env 门，故 corespine 套件每格都能确定性执行）；networkx 的可用性门由
# 上文 graph_store fixture 承载，二者职责不重叠（范式同 VECTOR_STORE_SUITE）。
# ===========================================================================
def _make_in_process_graph():
    """构造内存默认 GraphStore（延迟 import，红色阶段在此抛 ModuleNotFoundError）。"""
    from ragspine.graph.store import InProcessGraphStore

    return InProcessGraphStore()


GRAPH_STORE_REGISTRY: "Registry" = Registry("graph_store")
GRAPH_STORE_REGISTRY.register("in_process", _make_in_process_graph)


def _graph_node(node_id: str, ntype: str = "entity", *, sensitivity: str = "INTERNAL", **md):
    """构造带齐血缘的 GraphNode：默认 source_doc_id / source_locator / sensitivity，可逐项覆盖。

    延迟 import GraphNode 以保持惰性红色；供 InvariantPack 与领域断言共用同一构造口径。
    """
    from ragspine.graph.store import GraphNode

    meta = {
        "source_doc_id": md.pop("source_doc_id", "profile"),
        "source_locator": md.pop("source_locator", f"config#{node_id}"),
        "sensitivity": sensitivity,
    }
    meta.update({k: str(v) for k, v in md.items()})
    return GraphNode(id=node_id, type=ntype, label=node_id, metadata=meta)


def _graph_edge(src: str, dst: str, etype: str, **md):
    """构造带齐血缘的 GraphEdge：默认 source_doc_id / source_locator，可逐项覆盖（延迟 import）。"""
    from ragspine.graph.store import GraphEdge

    meta = {
        "source_doc_id": md.pop("source_doc_id", "profile"),
        "source_locator": md.pop("source_locator", f"config#{src}->{dst}"),
    }
    meta.update({k: str(v) for k, v in md.items()})
    return GraphEdge(src=src, dst=dst, type=etype, metadata=meta)


def _graph_determinism_repeated_traverse_stable(store) -> None:
    """同库同遍历两次：可达节点 id 序列逐位一致（确定性·机制）。"""
    store.upsert_nodes([_graph_node(f"n{i}") for i in range(8)])
    store.upsert_edges([_graph_edge("n0", f"n{i}", "rel") for i in range(1, 8)])
    first = [n.id for n in store.traverse("n0", max_depth=2)]
    second = [n.id for n in store.traverse("n0", max_depth=2)]
    assert first == second


def _graph_provenance_node_and_edge_round_trip(store) -> None:
    """节点/边血缘锚点 source_doc_id / source_locator 经 upsert->遍历原样回传（不臆造·机制）。"""
    store.upsert_nodes([
        _graph_node("GROUP"),
        _graph_node("HK", source_doc_id="HK_FIN.pptx", source_locator="HK_FIN.pptx!slide3"),
    ])
    store.upsert_edges([
        _graph_edge("GROUP", "HK", "parent_of",
                    source_doc_id="company.toml", source_locator="company.toml#home"),
    ])
    [hk] = store.neighbors("GROUP")
    assert hk.metadata["source_doc_id"] == "HK_FIN.pptx"
    assert hk.metadata["source_locator"] == "HK_FIN.pptx!slide3"
    [edge] = store.subgraph(["GROUP"], depth=1).edges
    assert edge.metadata["source_doc_id"] == "company.toml"
    assert edge.metadata["source_locator"] == "company.toml#home"


def _graph_isolation_restricted_never_surfaces(store) -> None:
    """RESTRICTED 来源节点绝不出现在 neighbors/traverse/subgraph，也不作跳板（隔离·机制）。"""
    store.upsert_nodes([
        _graph_node("A"), _graph_node("B"),
        _graph_node("SECRET", sensitivity="RESTRICTED"), _graph_node("C"),
    ])
    store.upsert_edges([
        _graph_edge("A", "B", "rel"), _graph_edge("B", "C", "rel"),
        _graph_edge("A", "SECRET", "rel"), _graph_edge("SECRET", "C", "rel"),
    ])
    assert "SECRET" not in {n.id for n in store.neighbors("A")}
    assert "SECRET" not in {n.id for n in store.traverse("A", max_depth=3)}
    sub = store.subgraph(["A"], depth=3)
    assert "SECRET" not in {n.id for n in sub.nodes}
    assert all("SECRET" not in (e.src, e.dst) for e in sub.edges)


GRAPH_STORE_INVARIANTS: "InvariantPack" = (
    InvariantPack("graph_store")
    .add("determinism_repeated_traverse_stable", _graph_determinism_repeated_traverse_stable)
    .add("provenance_node_and_edge_round_trip", _graph_provenance_node_and_edge_round_trip)
    .add("isolation_restricted_never_surfaces", _graph_isolation_restricted_never_surfaces)
)

# 实现 × 不变量 的笛卡尔积套件：corespine 负责「跑全套 + 定位坏格子」。每格新建实例，杜绝串味。
GRAPH_STORE_SUITE: "ConformanceSuite" = ConformanceSuite(
    {name: (lambda n=name: GRAPH_STORE_REGISTRY.make(n)) for name in GRAPH_STORE_REGISTRY.names()},
    GRAPH_STORE_INVARIANTS,
)


# ---------------------------------------------------------------------------
# 注册表：受 conformance 约束的 TraceSink 实现（B1 TraceSink 缝，🛡）。第三方实现在此追加一行
# + 在 _build_trace_sink 里补一行（或经 make_trace_sink 的 entry-point 自动发现）即继承整套隐私
# conformance pack——同 VectorStore / GraphStore 缝的范式。in_process 是 corespine 的进程内隐私
# 安全默认（构造即拒受限正文）；otel 是 behind [otel] extra 的薄 adapter（OtelTraceSink，扇出前
# 先过隐私门），由 fixture 的 importorskip 门控（缺 opentelemetry 则该参数 skip，黄不红）。
# ---------------------------------------------------------------------------
TRACE_SINK_IMPLS = ("in_process", "otel")


def _build_trace_sink(name: str):
    """名字 -> TraceSink 实例（延迟 import，红色阶段在此抛 ModuleNotFoundError）。"""
    from ragspine.common.observability.sink import make_trace_sink

    if name == "in_process":
        sink = make_trace_sink("in_process")
        assert sink is not None
        return sink
    if name == "otel":
        from ragspine.common.observability.adapters.otel import OtelTraceSink

        return OtelTraceSink()
    raise KeyError(name)


@pytest.fixture(params=list(TRACE_SINK_IMPLS), ids=list(TRACE_SINK_IMPLS))
def trace_sink(request):
    """每个注册 TraceSink 各给一个全新实例；每条隐私用例对所有实现各跑一遍。

    otel behind [otel] extra：缺 opentelemetry 时该参数 skip（黄，不红），装了即整套 gate。
    """
    if request.param == "otel":
        pytest.importorskip(
            "opentelemetry", reason="opentelemetry 未装（pip install rag-spine[otel]）"
        )
    return _build_trace_sink(request.param)


# ===========================================================================
# corespine 机制层 conformance 骨架（TraceSink 缝）——与上文 trace_sink fixture-形态参数化
# 两层互补，领域断言不外迁。范围刻意收敛在【离线零依赖、零服务】的 in_process 隐私默认上
# （无 importorskip / 服务门，故 corespine 套件每格都能确定性执行）；otel 的可用性门由上文
# trace_sink fixture 承载，二者职责不重叠（范式同 VECTOR_STORE_SUITE / GRAPH_STORE_SUITE）。
# ===========================================================================
def _make_in_process_trace_sink():
    """构造隐私安全默认 TraceSink（经 ragspine 缝的 make_trace_sink 解析；延迟 import 保惰性红色）。"""
    from ragspine.common.observability.sink import make_trace_sink

    sink = make_trace_sink("in_process")
    assert sink is not None
    return sink


TRACE_SINK_REGISTRY: "Registry" = Registry("trace_sink")
TRACE_SINK_REGISTRY.register("in_process", _make_in_process_trace_sink)


def _trace_rejects_forbidden_answer(sink) -> None:
    """含 answer 正文的载荷必须被拒绝（抛某 CorespineError 子类），绝不静默落库（隐私·机制）。"""
    from corespine import TraceError

    raised = False
    try:
        sink.emit("trace", request_id="r1", answer="机密答案 4500")
    except TraceError:
        raised = True
    assert raised, "含 answer 正文的载荷未被拒绝——隐私 trace 必须拒绝或擦除正文"


def _trace_rejects_fact_value(sink) -> None:
    """含 value 事实数值的载荷必须被拒绝（隐私·机制）。"""
    from corespine import TraceError

    raised = False
    try:
        sink.emit("trace", request_id="r1", value=4500.0)
    except TraceError:
        raised = True
    assert raised, "含 value 事实数值的载荷未被拒绝——隐私 trace 必须拒绝或擦除正文"


def _trace_rejects_chunk_text(sink) -> None:
    """含 chunk_text 正文的载荷必须被拒绝（隐私·机制）。"""
    from corespine import TraceError

    raised = False
    try:
        sink.emit("trace", request_id="r1", chunk_text="整段 chunk 正文")
    except TraceError:
        raised = True
    assert raised, "含 chunk_text 正文的载荷未被拒绝——隐私 trace 必须拒绝或擦除正文"


def _trace_records_only_metadata(sink) -> None:
    """允许的元数据（code/count/timing）emit 不抛，且记录里不含任何受限键（只记元数据·机制）。"""
    from corespine import FORBIDDEN_KEYS

    sink.emit("trace", request_id="r1", route="structured", n_hits=3, took_ms=12)
    events = getattr(sink, "events", [])
    assert events, "允许的元数据 emit 后应有记录"
    assert all(
        str(k).strip().lower() not in FORBIDDEN_KEYS for e in events for k in e.fields
    ), "记录里出现了受限键——只应记 code / 计数 / 耗时 元数据"


TRACE_SINK_INVARIANTS: "InvariantPack" = (
    InvariantPack("trace_sink")
    .add("rejects_forbidden_answer", _trace_rejects_forbidden_answer)
    .add("rejects_fact_value", _trace_rejects_fact_value)
    .add("rejects_chunk_text", _trace_rejects_chunk_text)
    .add("records_only_metadata", _trace_records_only_metadata)
)

# 实现 × 不变量 的笛卡尔积套件：corespine 负责「跑全套 + 定位坏格子」。每格新建实例，杜绝串味。
TRACE_SINK_SUITE: "ConformanceSuite" = ConformanceSuite(
    {name: (lambda n=name: TRACE_SINK_REGISTRY.make(n)) for name in TRACE_SINK_REGISTRY.names()},
    TRACE_SINK_INVARIANTS,
)


# ---------------------------------------------------------------------------
# 注册表：受 conformance 约束的 FactStore 实现（B2 FactStore 缝，🛡 结构化通路 = 反编造根基）。第三方
# 实现在此追加一行 + 在 _build_fact_store 里补一行（或经 make_fact_store 的 entry-point 自动发现）即
# 继承整套反编造 / provenance conformance pack——同 VectorStore / GraphStore / TraceSink 缝的范式。
# sqlite 是零依赖确定性默认（SqliteFactStore，stdlib，装了即跑、无 importorskip / env 门）；DuckDB /
# Postgres 为 follow-up（需外部依赖，behind 各自 extra，落地时在此加 importorskip / env 门，缺则 skip）。
# ---------------------------------------------------------------------------
FACT_STORE_IMPLS = ("sqlite",)


def _build_fact_store(name: str):
    """名字 -> 已 init_schema 的 FactStore 实例（内存库，延迟 import，红色阶段在此抛 ModuleNotFoundError）。"""
    from ragspine.storage.fact_store import make_fact_store

    if name == "sqlite":
        store = make_fact_store("sqlite", db_path=":memory:")
        assert store is not None
        store.init_schema()
        return store
    raise KeyError(name)


@pytest.fixture(params=list(FACT_STORE_IMPLS), ids=list(FACT_STORE_IMPLS))
def fact_store(request):
    """每个注册 FactStore 各给一个【全新空库 + 已 init_schema】实例；每条用例对所有实现各跑一遍。

    sqlite 是 stdlib 零依赖默认（装了即跑，无 importorskip / env 门）；DuckDB / Postgres follow-up 时
    在此按需加可用性门（缺则该参数 skip，黄不红，范式同 vector_store 夹具）。收尾统一 close 关连接。
    """
    store = _build_fact_store(request.param)
    yield store
    close = getattr(store, "close", None)
    if callable(close):
        close()


# ===========================================================================
# corespine 机制层 conformance 骨架（FactStore 缝）——与上文 fact_store fixture-形态参数化两层互补，
# 领域断言不外迁。范围收敛在【离线零依赖、零服务】的 sqlite 默认实现上（无 importorskip / 服务门，故
# corespine 套件每格都能确定性执行）。范式同 VECTOR_STORE_SUITE / GRAPH_STORE_SUITE / TRACE_SINK_SUITE。
# ===========================================================================
def _make_sqlite_fact_store():
    """构造已 init_schema 的 sqlite 默认 FactStore（经 make_fact_store 解析；延迟 import 保惰性红色）。"""
    from ragspine.storage.fact_store import make_fact_store

    store = make_fact_store("sqlite", db_path=":memory:")
    assert store is not None
    store.init_schema()
    return store


def _conformance_fact(**over):
    """构造一条带齐血缘的 Fact（延迟 import，供 InvariantPack 与领域断言共用同一构造口径）。"""
    from ragspine.storage.fact_store import Fact

    fields = dict(
        metric_code="REVENUE",
        entity="ACME_HK",
        geography="HK",
        channel="TOTAL",
        period_type="FY",
        period="2024",
        value=4500.0,
        unit="HKD_MN",
        source_doc_id="HK_FIN.pptx",
        source_locator="HK_FIN.pptx!slide3#para2",
    )
    fields.update(over)
    return Fact(**fields)


def _fact_found_determinism_stable(store) -> None:
    """命中→确定值（跨调用逐位一致 + 等于入库值）、未命中→空（绝不臆造）——反编造存储侧根基·机制。"""
    store.upsert_facts([_conformance_fact(value=4500.0)])
    first = store.query("REVENUE", "ACME_HK", "FY", "2024")
    second = store.query("REVENUE", "ACME_HK", "FY", "2024")
    assert first and second, "命中查询应确定返回一条 Fact（found 语义）"
    assert first[0].value == second[0].value == 4500.0, "命中值必须确定且等于入库值（不臆造/不漂移）"
    assert store.query("REVENUE", "ACME_HK", "FY", "2099") == [], "未命中必须返回空（绝不臆造一个值）"


def _fact_provenance_round_trip(store) -> None:
    """found 结果带 source_doc_id + source_locator，lineage 经 upsert/query 存活（不臆造 / 不丢·机制）。"""
    store.upsert_facts(
        [_conformance_fact(source_doc_id="HK_FIN.pptx", source_locator="HK_FIN.pptx!slide3#para2")]
    )
    hits = store.query("REVENUE", "ACME_HK", "FY", "2024")
    assert hits, "provenance 判定核未命中（应至少回传入库的一条 Fact）"
    for f in hits:
        assert f.source_doc_id == "HK_FIN.pptx", f"found 结果丢了 source_doc_id（血缘根）：{f!r}"
        assert (
            f.source_locator == "HK_FIN.pptx!slide3#para2"
        ), f"found 结果丢了 source_locator（citation 回指）：{f!r}"


FACT_STORE_REGISTRY: "Registry" = Registry("fact_store")
FACT_STORE_REGISTRY.register("sqlite", _make_sqlite_fact_store)

FACT_STORE_INVARIANTS: "InvariantPack" = (
    InvariantPack("fact_store")
    .add("found_determinism_stable", _fact_found_determinism_stable)
    .add("provenance_round_trip", _fact_provenance_round_trip)
)

# 实现 × 不变量 的笛卡尔积套件：corespine 负责「跑全套 + 定位坏格子」。每格新建实例，杜绝串味。
FACT_STORE_SUITE: "ConformanceSuite" = ConformanceSuite(
    {name: (lambda n=name: FACT_STORE_REGISTRY.make(n)) for name in FACT_STORE_REGISTRY.names()},
    FACT_STORE_INVARIANTS,
)
