"""服务层配置与资源装配的可执行规格（TDD）。

只验证外部行为：from_env 解析、build_provider 分流、store/retriever 资源开关、
ingestion 路径安全校验。不依赖网络、真实 LLM key 或 Redis。
"""

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from ragspine.agent.llm_provider import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicProvider,
    MockProvider,
)
from ragspine.service.config import (
    PathNotAllowedError,
    ServiceConfig,
    build_provider,
    open_fact_store,
    open_narrative_retriever,
    validate_ingest_path,
)


def test_from_env_defaults_when_empty():
    cfg = ServiceConfig.from_env({})
    assert cfg.db_path == "data/fact_metric.db"
    assert cfg.chunk_db_path is None
    assert cfg.mapping_db_path is None
    assert cfg.queue_db_path is None
    assert cfg.manifest_db_path is None
    assert cfg.redis_url == "redis://localhost:6379/0"
    assert cfg.provider_type == "mock"
    assert cfg.model == DEFAULT_ANTHROPIC_MODEL
    assert cfg.base_url is None
    # W1：默认 'auto'＝装了 [embed-onnx] 走真语义 ONNX、否则回落纯 BM25。lean 运行时行为不变
    # （extra 不在时 make_embedding_backend('auto') -> None，守 ADR 0005），仅默认配置字符串升级。
    assert cfg.embedding == "auto"
    assert cfg.workflow_matcher == "auto"
    assert cfg.vector_store == "none"
    assert cfg.persistence_policy == "default"
    assert cfg.reference_date is None
    assert cfg.faq_source is None
    assert cfg.allowed_upload_root is None
    assert cfg.company_profile_path is None


def test_from_env_parses_all_keys():
    env = {
        "RAGSPINE_DB_PATH": "/tmp/fact.db",
        "RAGSPINE_CHUNK_DB_PATH": "/tmp/chunk.db",
        "RAGSPINE_MAPPING_DB_PATH": "/tmp/map.db",
        "RAGSPINE_QUEUE_DB_PATH": "/tmp/review.db",
        "RAGSPINE_MANIFEST_DB_PATH": "/tmp/manifest.db",
        "RAGSPINE_REDIS_URL": "redis://example:6380/1",
        "RAGSPINE_PROVIDER": "anthropic",
        "RAGSPINE_MODEL": "claude-test",
        "RAGSPINE_BASE_URL": "https://gateway.example/v1",
        "RAGSPINE_EMBEDDING": "deterministic",
        "RAGSPINE_WORKFLOW_MATCHER": "none",
        "RAGSPINE_VECTOR_STORE": "in_process",
        "RAGSPINE_PERSISTENCE_POLICY": "persist_everything",
        "RAGSPINE_REFERENCE_DATE": "2026-06-12",
        "RAGSPINE_FAQ_SOURCE": "/tmp/faq.json",
        "RAGSPINE_ALLOWED_UPLOAD_ROOT": "/tmp/uploads",
        "RAGSPINE_COMPANY_PROFILE": "/tmp/company.json",
    }
    cfg = ServiceConfig.from_env(env)
    assert cfg.db_path == "/tmp/fact.db"
    assert cfg.chunk_db_path == "/tmp/chunk.db"
    assert cfg.mapping_db_path == "/tmp/map.db"
    assert cfg.queue_db_path == "/tmp/review.db"
    assert cfg.manifest_db_path == "/tmp/manifest.db"
    assert cfg.redis_url == "redis://example:6380/1"
    assert cfg.provider_type == "anthropic"
    assert cfg.model == "claude-test"
    assert cfg.base_url == "https://gateway.example/v1"
    assert cfg.embedding == "deterministic"
    assert cfg.workflow_matcher == "none"
    assert cfg.vector_store == "in_process"
    assert cfg.persistence_policy == "persist_everything"
    assert cfg.reference_date == "2026-06-12"
    assert cfg.faq_source == "/tmp/faq.json"
    assert cfg.allowed_upload_root == "/tmp/uploads"
    assert cfg.company_profile_path == "/tmp/company.json"


def test_config_is_frozen():
    cfg = ServiceConfig(db_path="/tmp/x.db")
    with pytest.raises(FrozenInstanceError):
        cfg.db_path = "/tmp/y.db"  # type: ignore[misc]


def test_reference_date_obj_parses_iso():
    cfg = ServiceConfig(db_path="/tmp/x.db", reference_date="2026-06-12")
    assert cfg.reference_date_obj() == date(2026, 6, 12)


def test_reference_date_obj_none_when_unset():
    cfg = ServiceConfig(db_path="/tmp/x.db")
    assert cfg.reference_date_obj() is None


def test_build_provider_mock_no_network(monkeypatch):
    # 断网：MockProvider 绝不触网
    cfg = ServiceConfig(db_path="/tmp/x.db", provider_type="mock", reference_date="2026-06-12")
    provider = build_provider(cfg)
    assert isinstance(provider, MockProvider)
    assert provider.reference_date == date(2026, 6, 12)


def test_build_provider_anthropic_type(monkeypatch):
    # anthropic SDK 属可选 [llm] 组：用桩件注入 sys.modules，离线也能验证装配。
    import sys
    import types

    created = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            created.update(kwargs)

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = _FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    cfg = ServiceConfig(
        db_path="/tmp/x.db",
        provider_type="anthropic",
        model="claude-test",
        base_url="https://gw/v1",
    )
    provider = build_provider(cfg)
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-test"
    assert created.get("base_url") == "https://gw/v1"


def test_build_provider_unknown_raises():
    cfg = ServiceConfig(db_path="/tmp/x.db", provider_type="bogus")
    with pytest.raises(ValueError):
        build_provider(cfg)


def test_open_fact_store_opens_and_closes(tmp_path):
    db = tmp_path / "fact.db"
    with open_fact_store(ServiceConfig(db_path=str(db))) as store:
        # schema 已初始化，store 可用
        assert store is not None
        assert hasattr(store, "close")
    # 退出后文件已落地
    assert db.exists()


def test_open_narrative_retriever_none_when_no_chunk_db():
    cfg = ServiceConfig(db_path="/tmp/x.db", chunk_db_path=None)
    provider = MockProvider()
    with open_narrative_retriever(cfg, provider) as retriever:
        assert retriever is None


def test_open_narrative_retriever_yields_and_closes(tmp_path):
    chunk_db = tmp_path / "chunk.db"
    cfg = ServiceConfig(
        db_path=str(tmp_path / "fact.db"),
        chunk_db_path=str(chunk_db),
        embedding="none",
    )
    provider = MockProvider()
    with open_narrative_retriever(cfg, provider) as retriever:
        assert retriever is not None
    assert chunk_db.exists()


def test_validate_ingest_path_accepts_allowed(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    f = root / "deck.xlsx"
    f.write_bytes(b"x")
    cfg = ServiceConfig(db_path="/tmp/x.db", allowed_upload_root=str(root))
    resolved = validate_ingest_path(str(f), cfg, suffixes=(".xlsx", ".pptx"))
    assert resolved == f.resolve()


def test_validate_ingest_path_rejects_outside_root(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    outside = tmp_path / "secret.xlsx"
    outside.write_bytes(b"x")
    cfg = ServiceConfig(db_path="/tmp/x.db", allowed_upload_root=str(root))
    with pytest.raises(PathNotAllowedError):
        validate_ingest_path(str(outside), cfg, suffixes=(".xlsx",))


def test_validate_ingest_path_rejects_traversal(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    (tmp_path / "secret.xlsx").write_bytes(b"x")
    cfg = ServiceConfig(db_path="/tmp/x.db", allowed_upload_root=str(root))
    traversal = str(root / ".." / "secret.xlsx")
    with pytest.raises(PathNotAllowedError):
        validate_ingest_path(traversal, cfg, suffixes=(".xlsx",))


def test_validate_ingest_path_rejects_bad_suffix(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    f = root / "deck.txt"
    f.write_bytes(b"x")
    cfg = ServiceConfig(db_path="/tmp/x.db", allowed_upload_root=str(root))
    with pytest.raises(PathNotAllowedError):
        validate_ingest_path(str(f), cfg, suffixes=(".xlsx", ".pptx"))


def test_validate_ingest_path_no_root_still_checks_suffix(tmp_path):
    # 未配置 allowed_upload_root 时仅做后缀校验
    f = tmp_path / "deck.xlsx"
    f.write_bytes(b"x")
    cfg = ServiceConfig(db_path="/tmp/x.db", allowed_upload_root=None)
    resolved = validate_ingest_path(str(f), cfg, suffixes=(".xlsx",))
    assert resolved == f.resolve()
    bad = tmp_path / "deck.bin"
    bad.write_bytes(b"x")
    with pytest.raises(PathNotAllowedError):
        validate_ingest_path(str(bad), cfg, suffixes=(".xlsx",))
