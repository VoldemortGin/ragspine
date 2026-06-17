"""vector —— 可注入的 embedding 后端实现 + 后端工厂（默认无后端 = 纯 BM25）。

实现 lexical 检索定义的 EmbeddingBackend 协议，按需懒加载第三方 SDK。

Submodules:
    embedding_backends.py — embedding 后端实现（EmbeddingBackend 协议）+ 后端工厂。
"""
