"""vector —— 向量通道：可插拔 VectorStore 缝 + 可注入 embedding 后端（默认无 = 纯 BM25）。

实现 lexical 检索定义的 EmbeddingBackend 协议，按需懒加载第三方 SDK；VectorStore 缝把
provenance / isolation / determinism 三项不变量绑死在存储层（见 tests/conformance）。

Submodules:
    embedding_backends.py — embedding 后端实现（EmbeddingBackend 协议）+ 后端工厂。
    store.py — 可插拔 VectorStore 缝 + 零依赖确定性内存默认实现（InProcessVectorStore）。
"""
