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
CHUNKER_IMPLS = ("default",)


def _build_chunker(name: str):
    """名字 -> Chunker 实例（延迟 import，红色阶段在此抛 ModuleNotFoundError）。"""
    if name == "default":
        from ragspine.retrieval.chunking.chunker import DefaultChunker

        return DefaultChunker()
    raise KeyError(name)


@pytest.fixture(params=list(CHUNKER_IMPLS), ids=list(CHUNKER_IMPLS))
def chunker(request):
    """每个注册 Chunker 各给一个实例；每条 provenance 用例对所有实现各跑一遍。"""
    return _build_chunker(request.param)


@pytest.fixture
def make_record():
    """构造 VectorRecord 的工厂夹具：默认带齐血缘 + 过滤元数据，可逐项覆盖。

    默认 metadata 含 doc_id / source_locator（provenance 锚点）与
    topic/entity/geography/period/language/sensitivity（过滤维度），口径同 StoredChunk。
    vector 接受任意可迭代数，统一转 float 元组。延迟 import VectorRecord 以保持惰性红色。
    """
    from ragspine.retrieval.vector.store import VectorRecord

    def _make(record_id: str, vector, **metadata) -> "VectorRecord":
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

    return _make
