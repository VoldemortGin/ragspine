"""Strict public configuration contract for the high-level workspace facade."""

import sqlite3
from contextlib import closing
from datetime import date

import pytest
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("preset", "recipe"),
    [
        (
            "economy",
            {
                "retrieval_mode": "economy",
                "embedding": "none",
                "vector_store": "none",
                "reranker": "none",
                "postprocessor": "none",
            },
        ),
        (
            "balanced",
            {
                "retrieval_mode": "hybrid",
                "embedding": "deterministic",
                "vector_store": "in_process",
                "reranker": "none",
                "postprocessor": "none",
            },
        ),
        (
            "quality",
            {
                "retrieval_mode": "hybrid",
                "embedding": "onnx",
                "vector_store": "in_process",
                "reranker": "cross_encoder",
                "postprocessor": "mmr,lost_in_middle,compress",
            },
        ),
    ],
)
def test_resolve_config_returns_stable_immutable_preset_recipe(preset, recipe):
    from ragspine.config import EffectivePlan, resolve_config

    plan = resolve_config(preset=preset)

    assert isinstance(plan, EffectivePlan)
    assert plan.config.profile == preset
    assert plan.config.retrieval.model_dump() == recipe
    with pytest.raises(ValidationError, match="frozen"):
        plan.config.profile = "economy"
    with pytest.raises(ValidationError, match="frozen"):
        plan.sources = ()


def test_resolve_config_records_a_source_for_every_effective_leaf():
    from ragspine.config import resolve_config

    plan = resolve_config(
        preset="balanced",
        config={"retrieval": {"embedding": "none", "vector_store": "none"}},
    )

    assert set(plan.source_map) == set(plan.leaf_values)
    assert plan.source_for("profile") == "preset"
    assert plan.source_for("retrieval.retrieval_mode") == "preset"
    assert plan.source_for("retrieval.embedding") == "config"
    assert plan.source_for("retrieval.vector_store") == "config"
    assert plan.source_for("graph.mode") == "default"


def test_default_indexing_contract_is_explicit_and_fingerprint_stable():
    """Implicit and explicit defaults describe the same persisted index."""
    from ragspine.config import resolve_config

    implicit = resolve_config()
    explicit = resolve_config(
        config={"indexing": {"chunker": "none", "max_chars": 480, "overlap_chars": 80}}
    )

    assert implicit.config.indexing == explicit.config.indexing
    assert implicit.config.indexing.chunker == "none"
    assert implicit.index_fingerprint == explicit.index_fingerprint
    assert len(implicit.index_fingerprint) == 64


@pytest.mark.parametrize(
    "indexing",
    [
        {"chunker": "parent_child"},
        {"max_chars": 481},
        {"overlap_chars": 79},
    ],
)
def test_each_indexing_setting_changes_the_fingerprint(indexing):
    from ragspine.config import resolve_config

    assert resolve_config(config={"indexing": indexing}).index_fingerprint != (
        resolve_config().index_fingerprint
    )


def test_runtime_only_retrieval_settings_do_not_change_the_index_fingerprint():
    from ragspine.config import resolve_config

    baseline = resolve_config(preset="balanced")
    runtime_override = resolve_config(
        preset="balanced", config={"retrieval": {"postprocessor": "mmr,lost_in_middle,compress"}}
    )

    assert baseline.index_fingerprint == runtime_override.index_fingerprint


def test_legacy_narrative_index_accepts_only_the_historical_default_contract(tmp_path):
    """An unlabelled old index is inferable only as the historical 480/80 default."""
    from ragspine.config import ReindexRequiredError, resolve_config
    from ragspine.config.workspace import WorkspaceIndexMetadata

    db_path = tmp_path / "knowledge.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("CREATE TABLE narrative_chunk (chunk_id TEXT NOT NULL)")
        connection.execute("INSERT INTO narrative_chunk VALUES ('legacy#c0')")

    metadata = WorkspaceIndexMetadata(db_path, tmp_path)
    metadata.init_schema()
    metadata.assert_compatible(resolve_config())
    with pytest.raises(ReindexRequiredError, match="chunking"):
        metadata.assert_compatible(
            resolve_config(config={"indexing": {"chunker": "parent_child"}})
        )


def test_claim_never_overwrites_a_different_fingerprint_after_stale_validation(
    tmp_path, monkeypatch
):
    """record performs its own transactional CAS instead of trusting an earlier assertion."""
    from ragspine.config import ReindexRequiredError, resolve_config
    from ragspine.config.workspace import WorkspaceIndexMetadata

    db_path = tmp_path / "knowledge.db"
    metadata = WorkspaceIndexMetadata(db_path, tmp_path)
    metadata.init_schema()
    first = resolve_config()
    second = resolve_config(config={"indexing": {"chunker": "parent_child"}})
    metadata.claim(first)

    # Model the stale-check window in the former assert-then-upsert implementation.
    monkeypatch.setattr(metadata, "assert_compatible", lambda _plan: None)
    with pytest.raises(ReindexRequiredError, match="chunking"):
        metadata.claim(second)

    with closing(sqlite3.connect(db_path)) as connection:
        stored = connection.execute(
            "SELECT fingerprint FROM ragspine_index_metadata WHERE singleton = 1"
        ).fetchone()
    assert stored == (first.index_fingerprint,)


def test_resolve_config_rejects_preset_with_legacy_profile():
    from ragspine.config import resolve_config

    with pytest.raises(ValueError, match="preset.*profile"):
        resolve_config(preset="balanced", profile="quality")


def test_config_rejects_unknown_root_and_nested_keys():
    from ragspine.config import RAGSpineConfig

    with pytest.raises(ValidationError, match="extra_forbidden"):
        RAGSpineConfig.model_validate({"retrievel": {}})

    with pytest.raises(ValidationError, match="extra_forbidden"):
        RAGSpineConfig.model_validate({"retrieval": {"embeding": "none"}})


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"profile": "fastest"}, "profile"),
        ({"graph": {"mode": "always"}}, "mode"),
        ({"retrieval": {"embedding": "magic"}}, "embedding"),
        ({"retrieval": {"reranker": "magic"}}, "reranker"),
        ({"generation": {"provider_type": "magic"}}, "provider_type"),
        ({"graph": {"max_communities": 0}}, "max_communities"),
        ({"graph": {"max_communities": 101}}, "max_communities"),
        ({"storage": {"knowledge_db": "../escape.db"}}, "knowledge_db"),
        ({"storage": {"knowledge_db": "..\\escape.db"}}, "knowledge_db"),
        ({"storage": {"knowledge_db": "NUL"}}, "knowledge_db"),
        ({"storage": {"knowledge_db": "facts."}}, "knowledge_db"),
        ({"generation": {"reference_date": "2026-07-21"}}, "reference_date"),
    ],
)
def test_config_rejects_values_outside_its_literal_contract(payload, field):
    from ragspine.config import RAGSpineConfig

    with pytest.raises(ValidationError, match=field):
        RAGSpineConfig.model_validate(payload)


def test_config_json_schema_describes_all_six_domains_and_enums():
    from ragspine.config import RAGSpineConfig

    schema = RAGSpineConfig.model_json_schema()

    assert set(schema["properties"]) == {
        "profile",
        "indexing",
        "retrieval",
        "graph",
        "generation",
        "security",
        "storage",
    }
    assert schema["properties"]["profile"]["enum"] == ["economy", "balanced", "quality"]

    definitions = schema["$defs"]
    assert set(definitions) >= {
        "RetrievalConfig",
        "IndexingConfig",
        "GraphConfig",
        "GenerationConfig",
        "SecurityConfig",
        "StorageConfig",
    }
    assert definitions["GraphConfig"]["properties"]["mode"]["enum"] == ["off", "auto"]
    assert definitions["IndexingConfig"]["properties"]["chunker"]["enum"] == [
        "none",
        "default",
        "layout",
        "parent_child",
        "sentence_window",
        "semantic",
        "laws",
        "qa",
        "book",
    ]


def test_local_maps_config_profile_overrides_and_graph(tmp_path):
    from ragspine import RAGSpine
    from ragspine.agent.llm_provider import MockProvider
    from ragspine.config import RAGSpineConfig

    config = RAGSpineConfig.model_validate(
        {
            "profile": "balanced",
            "retrieval": {"embedding": "none", "vector_store": "none"},
            "graph": {"mode": "auto", "max_communities": 17},
            "generation": {"provider_type": "mock", "reference_date": date(2026, 7, 21)},
            "security": {"allowed_upload_root": str(tmp_path)},
            "storage": {
                "knowledge_db": "facts.sqlite3",
                "mapping_db": "aliases.sqlite3",
                "review_db": "reviews.sqlite3",
                "graph_db": "relations.sqlite3",
            },
        }
    )

    with RAGSpine.local(tmp_path / "knowledge", config=config) as rag:
        assert rag.retrieval.retrieval_mode == "hybrid"
        assert rag.retrieval.embedding == "none"
        assert rag.retrieval.vector_store == "none"
        assert rag.graph == "auto"
        assert rag.graph_max_communities == 17
        assert isinstance(rag.provider, MockProvider)
        assert rag.provider.reference_date.isoformat() == "2026-07-21"
        assert rag.allowed_upload_root == tmp_path
        assert rag.db_path.name == "facts.sqlite3"
        assert rag.mapping_db_path.name == "aliases.sqlite3"
        assert rag.review_db_path.name == "reviews.sqlite3"
        assert rag.graph_db_path.name == "relations.sqlite3"


def test_local_legacy_arguments_remain_supported_without_config(tmp_path):
    from ragspine import RAGSpine

    with RAGSpine.local(tmp_path / "knowledge", profile="balanced", graph="auto") as rag:
        assert rag.retrieval.retrieval_mode == "hybrid"
        assert rag.retrieval.embedding == "deterministic"
        assert rag.graph == "auto"


def test_explicit_legacy_arguments_override_config_at_the_boundary(tmp_path):
    from ragspine import RAGSpine
    from ragspine.config import RAGSpineConfig
    from ragspine.facade import make_retrieval_preset

    config = RAGSpineConfig.model_validate(
        {
            "profile": "quality",
            "retrieval": {"embedding": "onnx"},
            "graph": {"mode": "auto"},
        }
    )
    explicit_retrieval = make_retrieval_preset("economy")

    with RAGSpine.local(
        tmp_path / "knowledge",
        config=config,
        profile="balanced",
        retrieval=explicit_retrieval,
        graph="off",
    ) as rag:
        # A fully assembled legacy preset remains the most explicit retrieval input.
        assert rag.retrieval is explicit_retrieval
        assert rag.graph == "off"


def test_explicit_profile_replaces_config_retrieval_overrides(tmp_path):
    from ragspine import RAGSpine
    from ragspine.config import RAGSpineConfig

    config = RAGSpineConfig.model_validate(
        {"profile": "quality", "retrieval": {"embedding": "onnx"}}
    )

    with RAGSpine.local(tmp_path / "knowledge", config=config, profile="economy") as rag:
        assert rag.retrieval.retrieval_mode == "economy"
        assert rag.retrieval.embedding == "none"
        assert rag.retrieval.vector_store == "none"


def test_security_upload_root_is_enforced_before_ingestion(tmp_path):
    from ragspine import RAGSpine
    from ragspine.config import RAGSpineConfig

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be ingested", encoding="utf-8")
    config = RAGSpineConfig.model_validate({"security": {"allowed_upload_root": str(allowed)}})

    with RAGSpine.local(tmp_path / "workspace", config=config) as rag:
        with pytest.raises(ValueError, match="outside allowed_upload_root"):
            rag.ingest(outside)


def test_security_upload_root_rejects_nested_symlink_escape(tmp_path):
    from ragspine import RAGSpine
    from ragspine.config import RAGSpineConfig

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be ingested", encoding="utf-8")
    (allowed / "link.txt").symlink_to(outside)
    config = RAGSpineConfig.model_validate({"security": {"allowed_upload_root": str(allowed)}})

    with RAGSpine.local(tmp_path / "workspace", config=config) as rag:
        with pytest.raises(ValueError, match="outside allowed_upload_root"):
            rag.ingest(allowed)
