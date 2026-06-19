"""Conformance 测试夹共享夹具：把每条用例参数化到【每一个注册的 VectorStore 实现】。

设计意图（落地 docs/prd-vector-store-seam.md 与 docs/prd-breadth-via-adapters.md）：
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
# 注册表：受 conformance 约束的 VectorStore 实现（仅登记【名字】，类延迟解析）。
# 第三方实现在此追加名字 + 在 _resolve_impl 里补一行即继承全套
# （将来可换成 entry-point 自动发现）。
# ---------------------------------------------------------------------------
VECTOR_STORE_IMPLS = ["in_process", "sqlite_vec", "pgvector"]


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
    raise KeyError(name)


@pytest.fixture(params=VECTOR_STORE_IMPLS, ids=VECTOR_STORE_IMPLS)
def vector_store(request):
    """每个注册实现各给一个【全新空库】实例；每条用例对所有实现各跑一遍。

    真实后端 behind extra / 需外部服务：缺时该参数 skip（黄，不红），齐备即整套 gate。
        sqlite_vec —— 需 [vector]（嵌入式，装了即跑）。
        pgvector   —— 需 [vector] + 环境变量 RAGSPINE_PG_URL 指向带 pgvector 扩展的 Postgres
                      （服务型后端，默认本地 CI 无 PG 故 skip；配了 URL 即整套 gate，本地实测见 README）。
    """
    if request.param == "sqlite_vec":
        pytest.importorskip("sqlite_vec", reason="sqlite-vec 未装（pip install ragspine[vector]）")
    if request.param == "pgvector":
        pytest.importorskip("pg8000", reason="pg8000 未装（pip install ragspine[vector]）")
        if not os.environ.get("RAGSPINE_PG_URL"):
            pytest.skip("RAGSPINE_PG_URL 未设（pgvector conformance 需带 pgvector 扩展的 Postgres）")
    store = _resolve_impl(request.param)()
    yield store
    close = getattr(store, "close", None)
    if callable(close):
        close()  # sqlite_vec / pgvector 收尾关连接（pgvector 的 TEMP 表随之 drop）


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
