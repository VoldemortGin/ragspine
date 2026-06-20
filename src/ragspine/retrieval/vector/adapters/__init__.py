"""adapters —— 第三方 VectorStore 适配器（vec0 等），延迟 import，behind [vector] extra。

每个适配器都跑同一套 tests/conformance（参数化），把 provenance / isolation / determinism
绑死在缝上——不通过 conformance 的适配器直接 CI 红，而非生产事故。零依赖默认仍是
ragspine.retrieval.vector.store.InProcessVectorStore。

Submodules:
    sqlite_vec.py — sqlite-vec（vec0 虚表）适配器：嵌入式持久化 + conformance-bound 精确实现。
    pgvector.py — pgvector（PostgreSQL）适配器：网络化 / 共享持久化，pg8000(BSD) 驱动，conformance-bound。
    qdrant.py — Qdrant（HNSW）适配器：local 模式进程内 / 可落盘，qdrant-client(Apache-2.0) 驱动，conformance-bound 的第一个 approximate 后端，带来「exact vs approximate」能力旗标。
"""

from ragspine import _lazy_submodules

__getattr__, __dir__ = _lazy_submodules(__name__, __path__)
